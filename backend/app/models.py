from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

class PlatformEnum(str, Enum):
    CNBLOGS = "cnblogs"
    JUEJIN = "juejin"
    CSDN = "csdn"
    CTO51 = "51cto"
    ZHIHU = "zhihu"
    WEIBO = "weibo"
    SINA_BLOG = "sina_blog"
    JIANSHU = "jianshu"
    WECHAT = "wechat"
    CUSTOM_URLS = "custom_urls"

class ExportFormatEnum(str, Enum):
    MARKDOWN = "md"
    PDF = "pdf"
    HTML = "html"
    ORIGINAL_HTML = "original_html"
    WORD = "docx"
    TXT = "txt"

class ArticleItem(BaseModel):
    id: str
    title: str
    author: str = ""
    publish_time: str = ""
    url: str = ""
    platform: str = ""
    summary: str = ""
    content_html: str = ""
    content_markdown: str = ""
    images: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    category: Optional[str] = None
    read_num: Optional[int] = 0
    like_count: Optional[int] = 0
    old_like_count: Optional[int] = 0
    author_avatar: Optional[str] = None
    share_count: Optional[int] = 0
    comment_count: Optional[int] = 0
    is_original: bool = False
    comments: List[Dict[str, Any]] = Field(default_factory=list)
    is_failed: bool = False
    error_reason: Optional[str] = None
    content_type: Optional[str] = Field(default=None, description="内容类型：article 知乎文章（全量） / answer 问题回答，用于分类展示")
    column_title: Optional[str] = Field(default=None, description="所属专栏名（文章的归类标签，可多选，用 、 分隔）")

    # 兼容旧数据：未设置 content_type 时，根据标题前缀推断（仅知乎历史数据）
    def get_content_type(self) -> str:
        if self.content_type:
            # 旧模型曾把专栏文章当作独立类型 column_article；
            # 新模型里「文章」是全量集合，专栏只是归类标签（看 column_title 字段）
            return "article" if self.content_type == "column_article" else self.content_type
        # 历史旧数据的标题前缀兼容：【专栏文章】也属于「文章」全量集合
        if self.title.startswith("【专栏】") or self.title.startswith("【专栏文章】") or self.title.startswith("【文章】"):
            return "article"
        if self.title.startswith("【回答】"):
            return "answer"
        return "article"

class TaskCreateRequest(BaseModel):
    platform: PlatformEnum
    target: str = Field(..., description="博主主页链接、博主ID或批量文章URL")
    export_formats: List[ExportFormatEnum] = Field(
        default=[
            ExportFormatEnum.MARKDOWN,
            ExportFormatEnum.PDF,
            ExportFormatEnum.HTML,
            ExportFormatEnum.WORD,
            ExportFormatEnum.TXT
        ],
        description="导出的格式列表"
    )
    enable_noise_filter: bool = Field(True, description="是否开启智能去噪（剔除广告、求赞、关注引流语）")
    remove_image_watermark: bool = Field(True, description="是否智能去除平台水印与溯源高清原图 (关闭则保留平台原样水印图)")
    use_cache: bool = Field(True, description="是否开启断点续爬与本地持久化缓存")
    download_images: bool = Field(True, description="是否将文章配图下载到本地并转为相对路径打包进 ZIP")
    max_articles: Optional[int] = Field(None, description="最大抓取篇数，None 表示抓取全部")
    start_index: Optional[int] = Field(1, description="起始文章序号，从 1 开始")
    end_index: Optional[int] = Field(None, description="结束文章序号，留空表示抓取到最后")
    author_name_override: Optional[str] = Field(None, description="自定义博主名称")
    wechat_cookie: Optional[str] = Field(None, description="微信公众号抓取凭证 Cookie (可选)")
    wechat_token: Optional[str] = Field(None, description="微信公众号抓取 Token (可选)")
    wechat_uin: Optional[str] = Field(None, description="微信阅读端 uin 凭证 (可选)")
    wechat_key: Optional[str] = Field(None, description="微信阅读端 key 私钥 (可选)")
    wechat_pass_ticket: Optional[str] = Field(None, description="微信阅读端 pass_ticket 票据 (可选)")
    wechat_appmsg_token: Optional[str] = Field(None, description="微信阅读端 appmsg_token 会话凭证 (可选)")
    include_comments: bool = Field(True, description="是否抓取并内嵌微信精选留言与互动统计")
    zhihu_cookie: Optional[str] = Field(None, description="知乎登录凭证 Cookie / z_c0 (可选，用于抓取个人主页全部文章与回答)")
    zhihu_content_types: Optional[List[str]] = Field(default=["articles", "answers"], description="知乎抓取内容类型 (默认抓取全部文章与回答)")
    # 两阶段导出专用：当用户在「检索文章清单」弹窗中勾选具体篇目后，
    # 前端把已选文章的完整元数据回传，后端直接使用这些元数据抓取正文并导出，
    # 避免把 URL 列表错判定为 custom_urls 而丢失 content_type / column_title 等分类信息
    articles_meta: Optional[List[Dict[str, Any]]] = Field(None, description="已选文章元数据列表，存在时直接按此列表导出")

class TaskStatusEnum(str, Enum):
    PENDING = "pending"
    FETCHING_LIST = "fetching_list"
    SCRAPING_ARTICLES = "scraping_articles"
    WAITING_CONFIRMATION = "waiting_confirmation"
    PAUSED = "paused"
    CLEANING = "cleaning"
    EXPORTING = "exporting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class TaskProgress(BaseModel):
    task_id: str
    platform: str
    target: str
    author_name: str = ""
    status: TaskStatusEnum = TaskStatusEnum.PENDING
    total_articles: int = 0
    current_article_index: int = 0
    current_article_title: str = ""
    progress_percent: float = 0.0
    message: str = "任务已创建，等待调度..."
    created_at: str = ""
    completed_at: Optional[str] = None
    export_files: Dict[str, str] = Field(default_factory=dict)
    articles_meta: List[Dict[str, Any]] = Field(default_factory=list)
    failed_articles: List[Dict[str, Any]] = Field(default_factory=list)
    success_articles: List[Dict[str, Any]] = Field(default_factory=list)
    error_message: Optional[str] = None
    is_cancelled: bool = False
    is_paused: bool = False
    is_interrupted_to_export: bool = False
    explanation: Optional[str] = None
    declared_count: Optional[int] = None
    category_name: Optional[str] = None
