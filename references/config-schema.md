# 配置与命令

脚本使用 JSON；省略字段时继承 `assets/default-config.json`。Logo、码图和字体的普通相对路径以配置文件目录解析。包内资产可用 `skill://assets/youzan-logo.png`，只允许解析到此 skill 目录内部的合法相对路径，拒绝路径穿越；移动工作配置不影响包内资产。脚本不联网。

| 字段 | 类型与说明 |
| --- | --- |
| `brand.name` / `tagline` | 品牌名和副文案，默认“有赞 / 做生意，找有赞” |
| `brand.logo_image` | 原始 Logo 图片；默认 `skill://assets/youzan-logo.png`，等比绘制；显式 `null` 才回退文字品牌 |
| `metrics` | 最多三项数字背书，元素包含 `value`、`label`；`source`、`verified_on`、`status` 保存证据，不印在底栏 |
| `statement_spans` | `reference-sentence` 必填的非空数组，每段含 `text` 字符串和可选 `bold` 布尔值；`text` 支持 `\n` 换行，数字段设 `bold: true`，其余段保持常规；最多排两行，过长时报错 |
| `phone.display` | 印在 PDF 上的电话，默认 `0571-8685 7988`；可读性分组不改变点击电话 URI |
| `phone.uri` | 可点击电话 URI，必须为 `tel:`，可带国家区号 |
| `phone.label` / `source` | 电话引导文案及参考来源 |
| `website.url` | 真实 HTTPS 官网 URL，同时用于点击和可选官网二维码 |
| `website.label` / `display` | 官网入口说明及显示域名；`grouped` 启用官网码时入口与码标签合并，关闭时使用独立文字入口；参考图布局始终有独立的胶囊按钮 |
| `website.button_label` | 可选胶囊按钮文字，仅用于两个参考图布局；省略时使用 `website.label`，建议“访问官网” |
| `qr.wecom` | `image` 原始企微码图路径，`label` 下方说明（参考图布局固定 20 字号、24 行距、最多两行居中；支持 `\n` 指定语义断行），`placeholder` 缺图提示，`link` 可选 HTTPS 点击地址 |
| `qr.mini_program` | 同上，使用原始小程序码，不将其内容转换为普通二维码 |
| `qr.website.enabled` | `true` 时增加第三个可扫官网二维码；默认 `true` |
| `qr.website.label` | 官网码下方说明，与其他码说明同字号；默认“扫码访问有赞官网”自动按“扫码访问 / 有赞官网”分两行 |
| `style.layout` | `grouped`（省略时兼容旧版）；`reference-columns`（Logo/三项数字在上，官网/电话胶囊在下，三码靠右）；`reference-sentence`（同样结构，但上排将指标串成一句话，数字加粗） |
| `style.endorsement_decoration` | 参考图布局的背书装饰：默认 `silver-laurel`（银灰月桂枝、深灰数字渐变），`none` 为纯文字；`grouped` 忽略此项。渐变文字仍可选择和搜索 |
| `style.height_ratio` | 底栏高 = 可见页宽 × 此值，默认 `0.125`（1440 × 180）；建议 `0.10–0.16` |
| `style.height_pt` | 非空时指定底栏高度 PDF point，优先于 ratio；底栏内容等比适应区域 |
| `style.background` / `accent` / `text` / `muted` / `line` | `#RRGGBB` 颜色；默认白底、品牌红入口、深灰数字、灰色说明、浅灰顶线 |
| `style.font_path` / `font_bold_path` | 可选支持中文的 `.ttf` 字体；省略时优先读取本机 NotoSansSC（macOS 用户 `~/Library/Fonts/NotoSansSC.ttf` 等路径），可用字体缺失时回退 ReportLab `STSong-Light`；纯英文数字使用 Helvetica |
| `output_suffix` | 目录批量的文件名后缀，默认 `-branded` |

建议每项数字 `value` 不超过 10 个全角字宽，`label` 不超过两行；数字自动拆分前缀、数值与单位，长说明以完整文字换行，不压缩成小字。默认数字和电话于 2026-10-22 从有赞官网核验，正式复用前根据时效更新证据。交易额是每年帮助商家完成的交易额，不能改写成有赞营收。

一句话版示例（此处仅展示覆盖字段，事实证据仍保留在 `metrics` 中）：

```json
{
  "style": {"layout": "reference-sentence"},
  "website": {"label": "访问官网"},
  "statement_spans": [
    {"text": "有赞拥有"},
    {"text": "13", "bold": true},
    {"text": "年社交电商经验，正在帮助"},
    {"text": "55,833", "bold": true},
    {"text": "个品牌，\n每年助力商家完成超"},
    {"text": "1,000", "bold": true},
    {"text": "亿交易额。"}
  ]
}
```

两个参考图布局共用相同位置的深灰官网按钮、常规字号电话、等尺寸三码和灰色码标签；电话号码不加粗，句子只将阿拉伯数字及数字内部的逗号等符号设为粗体。Logo 与码图均保留原图比例。`statement_spans` 的两行按 20 设计字号绘制，不通过横向拉伸或极小字号容纳过长文案。

```bash
# 单文件：output 是一个新 PDF 路径
python3 scripts/add_footer.py --input /data/article.pdf --output /data/output/article-branded.pdf --config /data/footer-config.json

# 批量：输出目录应位于输入目录之外
python3 scripts/add_footer.py --input /data/articles --output /data/branded --config /data/footer-config.json
```

可选 `--report /absolute/report.json` 写入每个文件的成功/失败、页数、待替换项和数字状态。无此参数时输出同样的 JSON 到标准输出。任一文件失败则退出状态为 `1`，其余文件仍会处理。写入使用临时文件再原子重命名，绝不覆盖现有输出。

输出保持原页面内容的矢量和尺寸，追加底栏后总页面高度变化。脚本会变换原链接点击区域和常见内部目的地坐标；不承诺保留依赖原页面坐标的 JavaScript、复杂多媒体或非标准交互。遇到表单或数字签名直接拒绝，保留源文件。

二维码缺图显示空白圆角框与文字提示，不生成看起来可扫的伪二维码。官网二维码由 ReportLab QR 编码器生成，并保留四模块安静区；实际成品仍需解码或扫码验证。
