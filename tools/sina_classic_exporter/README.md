# 🚀 新浪博客电脑端原版经典排版离线导出工具 (Sina Classic Exporter)

专门为追求 **“100% 还原当年在新浪博客电脑端看博文原版排版”** 的客户与重度读者定制打造的离线知识库导出工具。

---

## 🌟 核心特性

1. **原汁原味 1:1 复刻 PC 端经典双栏排版**：
   - 顶部经典蓝白横幅与博主签名；
   - 左侧经典博主名片卡与组件栏；
   - 右侧正文区完整保留新浪博客标志性的**折角博文小图标**、**粗宋体经典标题**、**发布时间**、**分类标签条**；
   - 严格还原宋体字阶、经典段前两字缩进与 `1.85` 行距，消除现代排版的违和感，完美唤醒读者的“视觉空间记忆”。
2. **左侧栏内置毫秒级离线即时搜索框**：
   - 支持拼音与汉字实时模糊过滤；
   - 输入关键词即时高亮匹配项，即时更新“显示 X / 总共 Y 篇”；
   - 点击左侧任意文章，右侧正文**无刷新平滑切换**；
   - 支持键盘 `←` / `→` 左右方向键快速切篇。
3. **精准文件名输出**：
   - 抓取分类专栏自动命名为：**`[分类名].html`**（如 **`西游正解.html`**）；
   - 绝不输出带有散乱随机字符的文件名。
4. **单文件 All-in-One 永久离线保存**：
   - 样式、脚本、正文配图（自动转为 Base64）全部封包在单一 `.html` 文件中；
   - 就算新浪博客下线或电脑彻底断网，双击即可在任何浏览器（Edge、Chrome、Safari 等）秒开阅读。

---

## 💻 命令行使用方式

### 1. 导出指定专栏（例如《西游正解》156篇）
```bash
python tools/sina_classic_exporter/export_classic_sina.py --url "https://blog.sina.com.cn/s/articlelist_5320406686_13_1.html"
```
产出文件：`downloads/西游正解.html`

### 2. 测试快速导出前 5 篇
```bash
python tools/sina_classic_exporter/export_classic_sina.py --url "https://blog.sina.com.cn/s/articlelist_5320406686_13_1.html" --max-articles 5
```

### 3. 自定义输出保存目录
```bash
python tools/sina_classic_exporter/export_classic_sina.py --url "https://blog.sina.com.cn/s/articlelist_5320406686_13_1.html" --output-dir "D:/我的电子书"
```

---

## 🛠️ 参数说明

| 参数 | 缩写 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `--url` | `-u` | **必填** | 新浪博客博主主页、博文目录或分类专栏 URL |
| `--output-dir` | `-o` | `downloads/` | 导出的 HTML 文件保存目录 |
| `--max-articles` | `-m` | `None` (全部) | 最大抓取文章数量限制 |
| `--no-inline-images` | | `False` | 不内联 Base64 图片（减小体积，但离线需联网看图） |
