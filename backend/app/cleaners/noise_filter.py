import re
from typing import List

# 常见引流、求关注、文末套话正则模式
NOISE_PATTERNS = [
    r"(?i)点击(?:上方|下方)?.*?关注(?:我们|公众号|作者)?",
    r"(?i)扫描(?:下方|上方)?二维码.*?关注",
    r"(?i)长按二维码.*?关注",
    r"(?i)本文(?:首发|首发于|原创于).*?(?:公众号|知乎|CSDN|掘金|博客园)",
    r"(?i)欢迎关注(?:我的)?(?:微信公众号|知乎|CSDN|掘金|GitHub|B站).*?",
    r"(?i)觉得(?:文章)?(?:有用|不错|有帮助)?.*?点赞.*?在看.*?转发",
    r"(?i)点赞.*?收藏.*?关注.*?三连",
    r"(?i)版权声明：本文为博主原创文章.*?",
    r"(?i)扫码回复.*?即可获取.*?",
    r"(?i)商务合作请联系.*?",
    r"(?i)未经作者授权.*?禁止转载.*?",
    r"(?i)加入技术交流群.*?加微信.*?",
    # 新浪博客与通用博客尾部/边栏干扰模式
    r"^分享：$",
    r"^喜欢\s*\d*$",
    r"^赠金笔$",
    r"^阅读[┊|].*",
    r"^收藏\s*\(javascript:;?\)",
    r"^.*┊打印.*",
    r"^.*举报/Report.*",
    r"^加载中，请稍候.*",
    r"^前一篇：.*",
    r"^后一篇：.*",
    r"^新浪BLOG意见反馈留言板.*",
    r"^新浪简介.*Copyright.*",
    r"^Copyright © .* SINA Corporation.*",
    r"^新浪公司\s*版权所有.*",
    r"^字体大小：.*[大中小].*",
    r"^顶\(\d+\)\s*踩\(\d+\)",
    r"^•\s*博客等级：",
    r"^•\s*博客积分：",
    r"^•\s*博客访问：",
    r"^•\s*获赠金笔：",
    r"^•\s*赠出金笔：",
    r"^•\s*荣誉徽章：",
    r"^加好友\s*\(javascript:.*\)",
    r"^发纸条\s*\(javascript:.*\)",
    r"^写留言\s*\(.*#write\)"
]

COMPILED_NOISE_REGEX = [re.compile(p) for p in NOISE_PATTERNS]

# 强阻断信号：一旦遇到这些文末垃圾行，后续全部截断
TRUNCATION_PATTERNS = [
    r"^前一篇：",
    r"^新浪BLOG意见反馈",
    r"^Copyright © \d{4} - \d{4} SINA Corporation",
    r"^新浪公司\s*版权所有",
]
COMPILED_TRUNCATION_REGEX = [re.compile(p) for p in TRUNCATION_PATTERNS]

def filter_noise_text(text: str) -> str:
    """
    智能过滤文章中的广告、引流套话、求赞求关注语句。
    保留干货观点与逻辑。
    """
    if not text:
        return ""
    
    lines = text.split("\n")
    cleaned_lines = []
    
    for line in lines:
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue
        
        # 检查是否遇到强阻断信号（文末彻底结束，如博客前一篇/版权栏）
        is_truncate = False
        for pattern in COMPILED_TRUNCATION_REGEX:
            if pattern.search(stripped):
                is_truncate = True
                break
        if is_truncate:
            break

        # 检查单行是否匹配任何噪音特征
        is_noise = False
        for pattern in COMPILED_NOISE_REGEX:
            if pattern.search(stripped):
                is_noise = True
                break
        
        # 过滤过短的纯引流标记行（例如单独一行 "[点赞]" 或 "【欢迎关注】"）
        if len(stripped) < 30 and ("关注" in stripped or "在看" in stripped or "二维码" in stripped or "交流群" in stripped):
            is_noise = True
            
        if not is_noise:
            cleaned_lines.append(line)
            
    result = "\n".join(cleaned_lines)
    # 去除连续超过 2 个的空行
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
