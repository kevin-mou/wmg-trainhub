import { EditorState } from "@codemirror/state";
import {
  EditorView,
  drawSelection,
  highlightActiveLine,
  highlightSpecialChars,
  keymap,
  placeholder,
} from "@codemirror/view";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import {
  bracketMatching,
  HighlightStyle,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { python, pythonLanguage } from "@codemirror/lang-python";
import { markdown } from "@codemirror/lang-markdown";
import { highlightTree, tagHighlighter, tags } from "@lezer/highlight";

const highlightStyle = HighlightStyle.define([
  { tag: tags.keyword, color: "var(--cm-keyword)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--cm-string)" },
  { tag: tags.comment, color: "var(--cm-comment)", fontStyle: "italic" },
  { tag: tags.number, color: "var(--cm-number)" },
  { tag: [tags.function(tags.variableName), tags.definition(tags.variableName)], color: "var(--cm-function)" },
  { tag: [tags.typeName, tags.className], color: "var(--cm-type)" },
  { tag: tags.operator, color: "var(--cm-operator)" },
  { tag: tags.heading, color: "var(--cm-keyword)", fontWeight: "600" },
  { tag: [tags.link, tags.url], color: "var(--cm-function)", textDecoration: "underline" },
  { tag: tags.emphasis, fontStyle: "italic" },
  { tag: tags.strong, fontWeight: "600" },
]);

const theme = EditorView.theme(
  {
    "&": { color: "var(--ink)", backgroundColor: "var(--surface)" },
    ".cm-content": {
      padding: "10px 0",
      fontFamily: "var(--font-mono)",
      fontSize: "12.5px",
      lineHeight: "1.6",
      caretColor: "var(--ink)",
    },
    ".cm-scroller": { fontFamily: "var(--font-mono)", overflow: "auto" },
    ".cm-line": { padding: "0 12px" },
    "&.cm-focused": { outline: "none" },
    ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--ink)" },
    ".cm-selectionBackground, ::selection": {
      backgroundColor: "color-mix(in srgb, var(--brass) 22%, transparent) !important",
    },
    ".cm-activeLine": {
      backgroundColor: "color-mix(in srgb, var(--brass) 6%, transparent)",
    },
    ".cm-gutters": { display: "none" },
    ".cm-placeholder": { color: "var(--ink-3)" },
    ".cm-tooltip": {
      backgroundColor: "var(--surface)",
      border: "1px solid var(--line)",
      color: "var(--ink)",
    },
  },
  { dark: false },
);

const previewHighlighter = tagHighlighter([
  { tag: tags.keyword, class: "nb-tok-keyword" },
  { tag: [tags.string, tags.special(tags.string)], class: "nb-tok-string" },
  { tag: tags.comment, class: "nb-tok-comment" },
  { tag: tags.number, class: "nb-tok-number" },
  { tag: [tags.bool, tags.atom], class: "nb-tok-atom" },
  {
    tag: [tags.function(tags.variableName), tags.definition(tags.variableName)],
    class: "nb-tok-function",
  },
  { tag: [tags.typeName, tags.className], class: "nb-tok-type" },
  { tag: tags.propertyName, class: "nb-tok-property" },
  { tag: tags.operator, class: "nb-tok-operator" },
]);

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) =>
    ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[char]
  );
}

export function renderCodePreview(code, language = "python") {
  if (language !== "python") return null;
  const source = code == null ? "" : String(code);
  try {
    const tree = pythonLanguage.parser.parse(source);
    let html = "";
    let pos = 0;
    highlightTree(tree, previewHighlighter, (from, to, classes) => {
      if (from > pos) html += escapeHtml(source.slice(pos, from));
      const text = escapeHtml(source.slice(from, to));
      html += classes ? `<span class="${classes}">${text}</span>` : text;
      pos = to;
    });
    if (pos < source.length) html += escapeHtml(source.slice(pos));
    return html;
  } catch {
    return null;
  }
}

function languageExtension(language) {
  if (language === "markdown") return markdown();
  if (language === "python") return python();
  return [];
}

export function createNotebookEditor({
  parent,
  value,
  language = "python",
  ariaLabel = "Notebook cell",
  placeholderText = "",
  onChange,
  onFocus,
  onBlur,
}) {
  const view = new EditorView({
    parent,
    state: EditorState.create({
      doc: value || "",
      extensions: [
        highlightSpecialChars(),
        history(),
        drawSelection(),
        EditorState.allowMultipleSelections.of(true),
        indentOnInput(),
        bracketMatching(),
        highlightActiveLine(),
        EditorView.lineWrapping,
        syntaxHighlighting(highlightStyle),
        placeholder(placeholderText),
        EditorView.contentAttributes.of({
          spellcheck: "false",
          autocorrect: "off",
          autocapitalize: "off",
          "aria-label": ariaLabel,
        }),
        theme,
        languageExtension(language),
        keymap.of([...defaultKeymap, ...historyKeymap, indentWithTab]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged && onChange) onChange(update.state.doc.toString());
          if (update.focusChanged) {
            if (update.view.hasFocus && onFocus) onFocus();
            if (!update.view.hasFocus && onBlur) onBlur();
          }
        }),
      ],
    }),
  });

  return {
    focus: () => view.focus(),
    destroy: () => view.destroy(),
    getValue: () => view.state.doc.toString(),
  };
}
