<p align="center">
  <img src="frontend/assets/logo_horizontal.png" alt="BlogDistiller (博萃)" width="800" style="max-width: 100%; border-radius: 12px; box-shadow: 0 10px 30px rgba(0,0,0,0.1);" />
</p>

<h1 align="center">📚 BlogDistiller (博萃)</h1>
<h3 align="center">全网博文批量下载与知识提纯在线工具 · 微信公众号 / 知乎 / 微博 / CSDN / 掘金文章一键打包导出</h3>

<p align="center">
  <a href="https://github.com/yibeigen/wechat-article-exporter"><img src="https://img.shields.io/badge/Web%20App-Online%20SaaS-009688?logo=googlechrome" alt="Web App"></a>
  <a href="https://yibeigen.pages.dev/"><img src="https://img.shields.io/badge/Author-艺杯羹-orange" alt="Author"></a>
  <a href="https://github.com/yibeigen/wechat-article-exporter/stargazers"><img src="https://img.shields.io/github/stars/yibeigen/wechat-article-exporter?style=flat&color=yellow" alt="GitHub Stars"></a>
  <img src="https://img.shields.io/badge/Platform-Web%20%7C%20Browser-lightgrey" alt="Platform">
</p>

<p align="center">
  <b>滤除网络杂质，沉淀纯粹知识。</b><br>
  无需安装复杂环境，打开浏览器即可使用的博文批量提取、去噪清洗与多格式离线归档工具。
</p>

---

## 🌟 为什么选择 BlogDistiller？

平时遇到喜欢的技术博主、专栏大牛或公众号，文章散落在各个平台，常常面临**文章被删、内容失效、广告引流套话多、离线无法阅读**等痛点。

**BlogDistiller (博萃)** 提供了一站式在线解决方案：
- 🚀 **全平台一键遍历**：支持输入博主主页链接、公众号名称或文章列表，秒级提取历史文章；
- 🧹 **智能去噪与去水印**：自动过滤“关注在看”、“点击上方蓝字”、“广告推荐”等营销引流文字，溯源高清无水印配图；
- 📖 **5 大格式合并打包**：自动编排结构化目录树（TOC），合并导出为单文件 **PDF、Markdown、离线网页、Word、纯文本**，并提供包含单篇独立文件的 **ZIP 归档大礼包**。

---

## 📱 支持平台与特性

| 平台 | 抓取方式 | 核心特性 | 适用场景 |
| :--- | :--- | :--- | :--- |
| **微信公众号** | 公众号名称 / 文章URL列表 / 专辑合集 | 官方超链接通道全量遍历 + 免登录批量提取 | 历史公号文章备份、合集打包 |
| **知乎 (Zhihu)** | 专栏主页 / 用户主页 / 文章回答 | 自动去噪、公式 MathML/LaTeX 保留、高赞回答归档 | 知乎神作合集、专栏技术书 |
| **微博 (Weibo)** | 博主主页 / 微博长文列表 | 微博头条长文提取、图片九宫格聚合 | 行业大 V 微博知识精选 |
| **CSDN 博客** | 博主主页 / 专栏目录 | 过滤关注博主弹窗，保留高亮代码块与图表 | 技术博客离线归档 |
| **稀土掘金** | 用户个人主页 | 深度遍历原创博文，纯净排版 | 前端/后端高质量掘金册子制作 |
| **博客园 (CNBlogs)** | 博客主页 | 深度解析博客园文章树 | 经典老牌开发者技术文章归档 |
| **51CTO 博客** | 博主专栏主页 | 分页遍历博主历史运维/架构文章 | IT 架构与运维知识库沉淀 |
| **自定义多链接** | 粘贴多条任意网页 URL | 跨站通用正文提取与聚合合并 | 散落网页/新闻/教程汇总成册 |

---

## 📦 5 种导出格式特性

1. **📝 Markdown (`.md`)**：
   - 带有顶部锚点目录树，自带标准 YAML Frontmatter 元数据（作者、发布时间、来源链接）；
   - 原生适配 **Dify / FastGPT / Obsidian / Notion / NotebookLM** AI 知识库与本地笔记。
2. **📄 高清排版文档 (`.pdf`)**：
   - 内置精美书籍封面、大纲书签索引、页码与页眉页脚；
   - 代码语法高亮，排版媲美出版社纸质书。
3. **🌐 独立离线电子书 (`.html`)**：
   - 单文件离线运行，内置自适应侧边栏目录与**全文实时搜索**；
   - 支持一键切换明暗主题与字体大小。
4. **📑 Word 文档 (`.docx`)**：
   - 原生 Word 大纲标题层级（Heading 1/2/3），表格与代码块完整保留，便于二次编辑与打印。
5. **📄 纯文本语料 (`.txt`)**：
   - 过滤所有 HTML 标签与复杂排版，适合快速分词、NLP 数据清洗与模型微调。
6. **📦 ZIP 完整归档大礼包**：
   - 一键打包全部 5 种格式合并文档 + 全部分篇独立文档 + `00_目录与索引清单.md`。

---

## ⚡ 三步在线使用流程

在线使用非常简单，无需在本地配置任何环境或敲命令行代码：

1. **选择平台与输入链接**：
   - 打开在线工作台，选择目标平台（如微信公众号、知乎、微博、CSDN 等），粘贴目标链接或公众号名称；
2. **设置导出选项**：
   - 勾选需要的导出格式（PDF / Markdown / HTML / Word / TXT），设置抓取范围（全部抓取或最新 N 篇）；
3. **一键启动与下载**：
   - 点击启动抓取，实时查看提取进度，任务完成后直接在界面一键下载合并单文件或完整 ZIP 大礼包！

---

## 🔑 免费获取激活口令

本在线工具完全免费开放使用，为了防止恶意爬虫刷量与机器人滥用：

1. 微信打开扫一扫，关注微信公众号【**艺杯羹**】（或微信搜一搜公众号 `艺杯羹`）；
2. 在公众号后台发送关键词：【**文章**】；
3. 即可秒得专属激活口令，在工作台输入后即可永久解锁全格式批量导出！

<p align="center">
  <img src="frontend/assets/wechat_qr.png" alt="微信公众号【艺杯羹】" width="360" style="border-radius: 8px;" />
</p>

---

## 👨‍💻 关于作者与矩阵生态

**博主：艺杯羹**  
*独立开发者 · 效率工具与知识系统创作者*

- 💬 **QQ 联系**：`3057454077`
- 📱 **微信交流**：`peace-83`
- 🌐 **个人主页**：[https://yibeigen.pages.dev/](https://yibeigen.pages.dev/)
- 📕 **CSDN 博客**：[博主 CSDN 主页](https://blog.csdn.net/qq_46987323?spm=1000.2115.3001.5343)
- 🌐 **个人微博**：[博主微博主页](https://www.weibo.com/u/7583841270)

### 🚀 博主其他精品项目：
- 🎯 **表达力训练平台**：[https://305758.xyz/](https://305758.xyz/) （结构化思维、即兴演讲与口才训练助手）
- 📖 **英语文章精读网站**：[http://cifan.305758.xyz](http://cifan.305758.xyz) （原汁原味英语时文精读与分级词汇解析）
- 🎙️ **语音跟随提词器**：[http://pip.305758.xyz](http://pip.305758.xyz) （自适应语速滚屏、录课口播智能提词神器）

<p align="center">
  <img src="frontend/assets/reward_qr.png" alt="赞赏支持" width="160" style="border-radius: 8px;" />
  <br>
  <sub>💖 如果觉得工具好用，欢迎赞赏支持作者持续迭代！</sub>
</p>

---

## 📄 免责声明 (Disclaimer)

1. 本工具仅供个人学习、离线研究与知识归档使用，严禁用于任何商业侵权或非法盗版传播行为；
2. 抓取的内容版权完全归原作者及原发布平台所有；
3. 请遵守各平台 Robots 协议与服务条款，合理控制抓取频率。

---

## 🔍 搜索引擎关键词 (SEO Keywords)

`微信公众号文章导出` · `公众号文章批量下载` · `公众号历史文章转PDF` · `知乎专栏批量导出` · `知乎回答导出Markdown` · `CSDN博客备份工具` · `微博长文批量下载` · `掘金文章离线保存` · `网页转Markdown` · `网页批量转PDF电子书` · `RAG知识库语料处理` · `BlogDistiller` · `艺杯羹`
