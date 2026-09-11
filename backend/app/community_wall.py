"""
社区致谢墙模块
管理：赞赏支持者、Bug 反馈者、功能建议者的展示与隐私设置
数据持久化到 backend/data/community_wall.json
"""

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field
from enum import Enum


class ContributorType(str, Enum):
    """贡献者类型"""
    SPONSOR = "sponsor"          # 赞赏支持
    BUG = "bug"                  # Bug 反馈
    FEATURE = "feature"          # 功能建议 / 共创


class PrivacyLevel(str, Enum):
    """隐私展示级别"""
    PUBLIC = "public"            # 显示头像 + 昵称
    NICKNAME_ONLY = "nickname"   # 仅显示昵称，头像用默认占位
    ANONYMOUS = "anonymous"      # 完全匿名，显示"热心网友"


class CommunityContributor(BaseModel):
    """社区贡献者数据模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: ContributorType = ContributorType.SPONSOR
    nickname: str = ""           # 昵称或真实称呼
    avatar_url: Optional[str] = None  # 头像链接，为空时使用默认头像
    privacy: PrivacyLevel = PrivacyLevel.PUBLIC
    description: str = ""        # 寄语、Bug 简述、建议摘要
    detail: str = ""             # 详细内容（Bug 详情/建议详情）
    amount: Optional[str] = None # 赞助档位，如"☕ 请喝咖啡"
    created_at: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    display_order: int = 0       # 展示顺序，数值越小越靠前


def _get_data_file() -> Path:
    """获取社区致谢墙 JSON 数据文件路径"""
    base = Path(__file__).resolve().parent.parent
    data_dir = base / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "community_wall.json"


def _load_raw() -> dict:
    """加载原始 JSON 数据"""
    file_path = _get_data_file()
    if not file_path.exists():
        return {"contributors": [], "updated_at": ""}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"contributors": [], "updated_at": ""}


def _save_raw(data: dict) -> None:
    """保存原始 JSON 数据"""
    file_path = _get_data_file()
    data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def list_contributors(contributor_type: Optional[ContributorType] = None) -> List[CommunityContributor]:
    """
    获取贡献者列表，按 display_order 升序、created_at 降序排列
    如果指定 contributor_type，则只返回该类型的贡献者
    """
    raw = _load_raw()
    contributors = raw.get("contributors", [])
    result = []
    for item in contributors:
        try:
            c = CommunityContributor(**item)
            if contributor_type is None or c.type == contributor_type:
                result.append(c)
        except Exception:
            continue
    result.sort(key=lambda x: (x.display_order, x.created_at), reverse=False)
    return result


def get_contributor(contributor_id: str) -> Optional[CommunityContributor]:
    """根据 ID 获取单个贡献者"""
    raw = _load_raw()
    for item in raw.get("contributors", []):
        if item.get("id") == contributor_id:
            return CommunityContributor(**item)
    return None


def add_contributor(contributor: CommunityContributor) -> CommunityContributor:
    """添加新的贡献者记录"""
    raw = _load_raw()
    contributors = raw.get("contributors", [])
    contributor.id = str(uuid.uuid4())[:8]
    if not contributor.created_at:
        contributor.created_at = datetime.now().strftime("%Y-%m-%d")
    contributors.append(contributor.dict())
    _save_raw(raw)
    return contributor


def update_contributor(contributor_id: str, contributor: CommunityContributor) -> Optional[CommunityContributor]:
    """更新贡献者记录"""
    raw = _load_raw()
    contributors = raw.get("contributors", [])
    for idx, item in enumerate(contributors):
        if item.get("id") == contributor_id:
            contributor.id = contributor_id
            contributors[idx] = contributor.dict()
            _save_raw(raw)
            return contributor
    return None


def delete_contributor(contributor_id: str) -> bool:
    """删除贡献者记录"""
    raw = _load_raw()
    contributors = raw.get("contributors", [])
    for idx, item in enumerate(contributors):
        if item.get("id") == contributor_id:
            contributors.pop(idx)
            _save_raw(raw)
            return True
    return False


def get_wall_summary() -> dict:
    """获取致谢墙统计摘要，用于前端展示"""
    all_contributors = list_contributors()
    public_data = []
    for c in all_contributors:
        # 根据隐私级别处理展示字段
        display_nickname = c.nickname
        display_avatar = c.avatar_url
        if c.privacy == PrivacyLevel.ANONYMOUS:
            display_nickname = "热心网友"
            display_avatar = None
        elif c.privacy == PrivacyLevel.NICKNAME_ONLY:
            display_avatar = None

        public_data.append({
            "id": c.id,
            "type": c.type.value,
            "nickname": display_nickname,
            "avatar_url": display_avatar,
            "description": c.description,
            "amount": c.amount,
            "created_at": c.created_at,
            "display_order": c.display_order
        })

    sponsors = [c for c in public_data if c["type"] == ContributorType.SPONSOR.value]
    bugs = [c for c in public_data if c["type"] == ContributorType.BUG.value]

    return {
        "sponsors": sponsors,
        "bugs": bugs,
        "total": len(public_data),
        "updated_at": _load_raw().get("updated_at", "")
    }


def seed_demo_data() -> None:
    """
    初始化示例数据（仅在空数据时执行）
    用于首次部署后让用户看到效果，后续可在管理后台替换
    """
    raw = _load_raw()
    if raw.get("contributors"):
        return

    demo = [
        CommunityContributor(
            type=ContributorType.SPONSOR,
            nickname="雪夜思红颜",
            privacy=PrivacyLevel.PUBLIC,
            description="楼主真给力，耐心解决问题。",
            amount="💰 100.00",
            created_at="2026-09-09",
            display_order=0
        )
    ]
    raw["contributors"] = [c.dict() for c in demo]
    _save_raw(raw)
