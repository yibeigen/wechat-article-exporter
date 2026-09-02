import os
import sys
import re
import ssl
import time
import socket
import select
import threading
import urllib.parse
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import httpx
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtensionOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
import datetime

from app.config import BASE_DIR
from app.core.wechat_auth import save_wechat_client_auth

SNIFFER_DIR = BASE_DIR / "data" / "certs"
SNIFFER_DIR.mkdir(parents=True, exist_ok=True)

CA_KEY_PATH = SNIFFER_DIR / "ca.key.pem"
CA_CERT_PATH = SNIFFER_DIR / "ca.cert.pem"

CA_COMMON_NAME = "BlogDistiller WeChat Intercept CA"
DEFAULT_PROXY_PORT = 8899

class CertManager:
    """自签根证书与动态域名证书签发管理器 (对齐公号三刀 node-forge 证书体系)"""

    def __init__(self):
        self.ca_key = None
        self.ca_cert = None
        self._ensure_ca_cert()

    def _ensure_ca_cert(self):
        if CA_KEY_PATH.exists() and CA_CERT_PATH.exists():
            try:
                with open(CA_KEY_PATH, "rb") as f:
                    self.ca_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
                with open(CA_CERT_PATH, "rb") as f:
                    self.ca_cert = x509.load_pem_x509_certificate(f.read(), backend=default_backend())
                return
            except Exception as e:
                print(f"读取已有 CA 证书异常，重新生成: {e}")

        # 生成 2048 位 RSA 根密钥
        self.ca_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "Beijing"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BlogDistiller"),
            x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME),
        ])

        now = datetime.datetime.now(datetime.timezone.utc)
        self.ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))  # 10 年有效
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None),
                critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False
                ),
                critical=True
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self.ca_key.public_key()),
                critical=False
            )
            .sign(self.ca_key, hashes.SHA256(), default_backend())
        )

        with open(CA_KEY_PATH, "wb") as f:
            f.write(self.ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption()
            ))

        with open(CA_CERT_PATH, "wb") as f:
            f.write(self.ca_cert.public_bytes(serialization.Encoding.PEM))

        print(f"✨ 新的自签根证书已生成: {CA_CERT_PATH}")

    def generate_host_cert(self, hostname: str) -> Tuple[bytes, bytes]:
        """为特定域名动态签发 SSL 证书"""
        host_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )

        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "BlogDistiller Sniffer"),
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        ])

        now = datetime.datetime.now(datetime.timezone.utc)
        
        # 兼容泛域名与主域名
        alt_names = [x509.DNSName(hostname)]
        if not hostname.startswith("*.") and "." in hostname:
            alt_names.append(x509.DNSName(f"*.{hostname}"))

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.ca_cert.subject)
            .public_key(host_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName(alt_names),
                critical=False
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True
            )
            .sign(self.ca_key, hashes.SHA256(), default_backend())
        )

        key_pem = host_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        return key_pem, cert_pem

    def install_to_windows_root(self) -> bool:
        """调用 Windows 原生 certutil 静默将 CA 证书安装进 CurrentUser 信任库"""
        if sys.platform != "win32":
            print("非 Windows 系统，请手动安装 CA 证书至系统根证书库")
            return False
        
        cmd = f'certutil.exe -user -addstore Root "{str(CA_CERT_PATH)}"'
        try:
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode == 0 or "成功" in res.stdout or "already" in res.stdout or "Command completed successfully" in res.stdout:
                print("✅ CA 根证书已成功注入 Windows 受信任根证书存储库 (Root)")
                return True
            else:
                print(f"注入根证书输出: {res.stdout} {res.stderr}")
                return True
        except Exception as e:
            print(f"安装证书异常: {e}")
            return False


class WeChatSnifferProxy:
    """微信阅读流量定向嗅探与拦截代理 (127.0.0.1:8899)"""

    def __init__(self, port: int = DEFAULT_PROXY_PORT, sync_remote_url: Optional[str] = None):
        self.port = port
        self.sync_remote_url = sync_remote_url or "https://doc.305758.xyz/api/wechat/set-auth"
        self.cert_manager = CertManager()
        self.running = False
        self.server_socket = None
        self._host_certs_cache = {}
        self.last_captured: Optional[Dict[str, Any]] = None

    def start(self, daemon: bool = True):
        self.cert_manager.install_to_windows_root()
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("127.0.0.1", self.port))
        self.server_socket.listen(128)
        self.running = True
        print(f"🚀 微信阅读嗅探代理已在 127.0.0.1:{self.port} 启动监听！")
        print(f"💡 操作提示：请在电脑微信中随意点开任意一篇公众号文章，嗅探器将全自动截获私钥凭证。")

        if daemon:
            t = threading.Thread(target=self._listen_loop, daemon=True)
            t.start()
        else:
            self._listen_loop()

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass

    def _listen_loop(self):
        while self.running:
            try:
                client_sock, addr = self.server_socket.accept()
                threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True).start()
            except Exception:
                if not self.running:
                    break

    def _handle_client(self, client_sock: socket.socket):
        try:
            req_data = client_sock.recv(4096)
            if not req_data:
                client_sock.close()
                return

            req_str = req_data.decode("utf-8", errors="ignore")
            lines = req_str.split("\r\n")
            if not lines or not lines[0]:
                client_sock.close()
                return

            first_line = lines[0]
            parts = first_line.split(" ")
            if len(parts) < 2:
                client_sock.close()
                return

            method, target = parts[0], parts[1]

            if method == "CONNECT":
                # HTTPS 隧道
                host_port = target.split(":")
                host = host_port[0]
                port = int(host_port[1]) if len(host_port) > 1 else 443

                # 如果是微信公众号流量 mp.weixin.qq.com 或 *.qq.com，进行 MITM 解密嗅探
                if "weixin.qq.com" in host or "mp.weixin.qq.com" in host:
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    self._handle_mitm_tls(client_sock, host, port)
                else:
                    # 普通直连隧道透传
                    try:
                        remote_sock = socket.create_connection((host, port), timeout=10)
                        client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        self._pipe_sockets(client_sock, remote_sock)
                    except Exception:
                        client_sock.close()
            else:
                # HTTP 明文请求透传与嗅探
                self._sniff_http_request(req_str)
                client_sock.close()

        except Exception as e:
            try:
                client_sock.close()
            except Exception:
                pass

    def _get_ssl_context(self, hostname: str) -> ssl.SSLContext:
        if hostname not in self._host_certs_cache:
            key_pem, cert_pem = self.cert_manager.generate_host_cert(hostname)
            key_file = SNIFFER_DIR / f"{hostname}.key"
            cert_file = SNIFFER_DIR / f"{hostname}.crt"
            with open(key_file, "wb") as f:
                f.write(key_pem)
            with open(cert_file, "wb") as f:
                f.write(cert_pem)
            self._host_certs_cache[hostname] = (str(cert_file), str(key_file))

        cert_file, key_file = self._host_certs_cache[hostname]
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=cert_file, keyfile=key_file)
        return ctx

    def _handle_mitm_tls(self, client_sock: socket.socket, host: str, port: int):
        try:
            ctx = self._get_ssl_context(host)
            ssl_client = ctx.wrap_socket(client_sock, server_side=True)
            
            # 读取客户端请求
            req_data = ssl_client.recv(8192)
            if req_data:
                req_str = req_data.decode("utf-8", errors="ignore")
                self._sniff_http_request(req_str, host)

            # 转发至腾讯真实目标服务器
            remote_raw = socket.create_connection((host, port), timeout=12)
            remote_ctx = ssl.create_default_context()
            ssl_remote = remote_ctx.wrap_socket(remote_raw, server_hostname=host)

            if req_data:
                ssl_remote.sendall(req_data)

            self._pipe_sockets(ssl_client, ssl_remote)
        except Exception as e:
            try:
                client_sock.close()
            except Exception:
                pass

    def _sniff_http_request(self, req_str: str, host: str = "mp.weixin.qq.com"):
        """从请求头与 URL 中提取微信凭证 (uin, key, pass_ticket, appmsg_token, wap_sid2, biz)"""
        try:
            lines = req_str.split("\r\n")
            first_line = lines[0] if lines else ""
            headers = {}
            for line in lines[1:]:
                if ": " in line:
                    k, v = line.split(": ", 1)
                    headers[k.lower()] = v.strip()

            cookie_hdr = headers.get("cookie", "")
            cookie_dict = {}
            if cookie_hdr:
                for item in cookie_hdr.split(";"):
                    item = item.strip()
                    if "=" in item:
                        ck, cv = item.split("=", 1)
                        cookie_dict[ck.strip()] = cv.strip()

            # 从 URL 中提取 Query 参数
            path_part = first_line.split(" ")[1] if len(first_line.split(" ")) > 1 else ""
            parsed = urllib.parse.urlparse(path_part)
            params = urllib.parse.parse_qs(parsed.query)

            uin = (
                params.get("uin", [""])[0]
                or cookie_dict.get("malluin", "")
                or cookie_dict.get("uin", "")
            )
            key = (
                params.get("key", [""])[0]
                or cookie_dict.get("mallkey", "")
                or cookie_dict.get("key", "")
            )
            pass_ticket = (
                params.get("pass_ticket", [""])[0]
                or cookie_dict.get("pass_ticket", "")
            )
            appmsg_token = (
                params.get("appmsg_token", [""])[0]
                or cookie_dict.get("appmsg_token", "")
            )
            wap_sid2 = cookie_dict.get("wap_sid2", "")
            biz = params.get("__biz", [""])[0]

            # 核心判定：必须包含有效的 pass_ticket 与 (uin 或 key)
            if pass_ticket and (key or uin or appmsg_token):
                captured = {
                    "uin": uin,
                    "key": key,
                    "pass_ticket": pass_ticket,
                    "appmsg_token": appmsg_token,
                    "wap_sid2": wap_sid2,
                    "biz": biz,
                    "rawCookie": cookie_hdr,
                    "captured_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                self.last_captured = captured
                
                # 本地持久化保存
                save_wechat_client_auth(
                    uin=uin,
                    key=key,
                    pass_ticket=pass_ticket,
                    appmsg_token=appmsg_token,
                    wap_sid2=wap_sid2,
                    biz=biz,
                    raw_cookie=cookie_hdr
                )

                print("\n" + "="*60)
                print("🎉【嗅探成功】已捕获到电脑微信阅读端核心凭证！")
                print(f"🔑 uin:         {uin[:10]}***" if uin else "🔑 uin:         (无)")
                print(f"🔑 key:         {key[:10]}***" if key else "🔑 key:         (无)")
                print(f"🔑 pass_ticket: {pass_ticket[:10]}***" if pass_ticket else "🔑 pass_ticket: (无)")
                print(f"🔑 appmsg_token:{appmsg_token[:10]}***" if appmsg_token else "🔑 appmsg_token:(无)")
                if biz:
                    print(f"🏷️ __biz:       {biz}")
                print("="*60 + "\n")

                # 异步同步至云端生产服务器
                if self.sync_remote_url:
                    threading.Thread(target=self._sync_to_remote, args=(captured,), daemon=True).start()

        except Exception as e:
            print(f"嗅探解析异常: {e}")

    def _sync_to_remote(self, payload: dict):
        """同步凭证至云端生产后端"""
        try:
            resp = httpx.post(self.sync_remote_url, json=payload, timeout=8.0)
            if resp.status_code == 200:
                print(f"☁️ 微信阅读凭证已自动同步至云端生产服务器: {self.sync_remote_url}")
        except Exception as e:
            pass

    def _pipe_sockets(self, sock1: socket.socket, sock2: socket.socket):
        sockets = [sock1, sock2]
        while self.running:
            rlist, _, xlist = select.select(sockets, [], sockets, 15)
            if xlist or not rlist:
                break
            for s in rlist:
                other = sock2 if s is sock1 else sock1
                try:
                    data = s.recv(16384)
                    if not data:
                        return
                    other.sendall(data)
                except Exception:
                    return
        try:
            sock1.close()
            sock2.close()
        except Exception:
            pass
