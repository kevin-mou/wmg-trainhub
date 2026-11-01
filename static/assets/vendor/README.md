# Vendored frontend bundles

TrainHub 的前端没有运行时构建链（static SPA 直接由 FastAPI 托管）。下面两个文件是
**预先构建好的 ESM bundle**，避免节点离线时依赖 CDN / npm：

| 文件 | 内容 | 构建入口 |
|------|------|----------|
| `trainhub-codemirror.js` | CodeMirror 6（编辑器、Python/Markdown 语言、高亮主题、只读代码预览高亮） | `cm-entry.js` |
| `trainhub-markdown.js` | marked + DOMPurify（Markdown 预览渲染与清理） | `markdown-entry.js` |

## 版本

构建时使用的依赖版本（`package.json`）：

- `codemirror` 6.x
- `@codemirror/state` / `view` / `commands` / `language` / `lang-python` / `lang-markdown` 6.x
- `@lezer/highlight` 1.x
- `marked` 18.x
- `dompurify` 3.x
- `esbuild` 0.28.x

许可证文本在 `licenses/` 目录。

## 重新构建

在任意临时目录安装依赖：

```bash
npm install codemirror @codemirror/state @codemirror/view @codemirror/commands \
  @codemirror/language @codemirror/lang-python @codemirror/lang-markdown \
  @lezer/highlight marked dompurify esbuild
npx esbuild cm-entry.js --bundle --format=esm --minify --target=es2020 \
  --outfile=trainhub-codemirror.js
npx esbuild markdown-entry.js --bundle --format=esm --minify --target=es2020 \
  --outfile=trainhub-markdown.js
```

把两个产物和对应 license 放回本目录。构建入口只暴露：

- `createNotebookEditor({parent, value, language, onChange, onFocus, onBlur})`
- `renderCodePreview(code, language?)`（返回已转义的 Python 高亮 HTML；非 Python 返回 `null`）
- `renderMarkdown(text)`（输出已经过 DOMPurify 清理）

当前版本由 `static/assets/pages/notebook-editor.js` 按需动态加载；未进入编辑器时不会下载这些 bundle。
