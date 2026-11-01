# Creating and installing language packs

[Tutorial index](../README.md) · [Complete user manual](Manual.en.md) · [简体中文](../zh/语言包指南.md)

A language is identified by its **folder name**. Each language needs only one UTF-8 JSON file. You do not need to change Python code, install translation software, or compile a language pack.

```text
config/languages/
├── zh-CN/strings.json
├── en-US/strings.json
└── ja-JP/strings.json
```

## Installing, selecting, and editing

1. Open **Settings → Personalization → Appearance** and click **Open language packs folder**. This opens the `config/languages` folder that the application actually reads. Release packages include the default Chinese and English files.
2. Copy an existing language folder and rename it to the desired language code, such as `ja-JP`, `fr-FR`, `pt-BR`, or `zh-Hant`.
3. Edit its `strings.json`: keep the source text on the left and enter translations on the right. Set `_meta.name` to the language's own name, such as `日本語`.
4. Return to Settings, click **Refresh list**, select the new language, and restart the application.

Restart after editing the current language pack as well. Switching languages does not rebuild pages that are running imports, deployments, or downloads.

Folder names may contain letters, digits, `-`, and `_`, must start with a letter, and may be up to 64 characters long. Standard language codes with hyphens are recommended; underscore forms such as `en_US` are also recognized. Do not create multiple folders for the same language that differ only in letter case or in the use of hyphens and underscores.

## File format

```json
{
  "$schema": "../schema.json",
  "_meta": {
    "format": 1,
    "name": "日本語",
    "author": "Your name",
    "fallback": "en-US"
  },
  "messages": {
    "设置": "設定",
    "搜索当前页面": "このページを検索",
    "正在导入 {count} 个 Mod": "{count} 個の Mod をインポート中"
  },
  "gamebanana": {
    "Skins": "スキン",
    "Characters": "キャラクター",
    "Weapons": "武器"
  },
  "qt": {
    "Cancel": "キャンセル",
    "&Yes": "はい(&Y)",
    "&No": "いいえ(&N)"
  }
}
```

- `messages`: application source text → translation, covering pages, notifications, dialogs, task progress, settings, and shortcut descriptions.
- `gamebanana`: original names returned by GameBanana → translated names. Category breadcrumbs, category selection lists, and type badges use these entries. You can add new website categories directly. Matching is case-insensitive. API responses, caches, and automatic installation matching retain the original names.
- `qt`: source text for Qt standard buttons and other controls → translation. Common button translations are supplied, and the application also loads the official Qt translation for the selected language when available. Plugins using Qt's standard translation API can read `messages`.
- `_meta.name`: the display name in the language list; it does not determine the language's identity. `author` is optional.
- `_meta.fallback`: an optional fallback language. You can translate only part of a pack and use this language for the remaining entries. Without a fallback, application text uses its source text and GameBanana names use their original API names.
- `$schema`: an optional reference to [schema.json](../../../config/languages/schema.json) in the language configuration folder. Editors with JSON Schema support, such as VS Code, use it to suggest fields and check the structure and field types, for example requiring translations in `messages` to be strings. The schema neither stores translations nor identifies the language. The application does not read it when loading a language pack; runtime validation is performed by the program. Language pack authors do not need to edit this file.

A simple top-level dictionary such as `{ "原文": "Translation" }` is also supported and is equivalent to providing only `messages`. Use the sectioned format above to translate GameBanana names or standard buttons.

Use the bundled [Chinese language pack](../../../config/languages/zh-CN/strings.json) and [English language pack](../../../config/languages/en-US/strings.json) as references for the file structure and existing translations.

An empty translation means “not translated yet” and falls back automatically. Do not repeat JSON keys or leave a trailing comma after the last item. UTF-8 files with a BOM are supported.

## Dynamic content, line breaks, and rich text

The application fills in values for `{count}`, `{name}`, `{value}`, and other placeholders. You may move a complete placeholder within a sentence, but **do not change anything inside it**, including format specifications and conversion flags such as `{count:,}`, `{size:.1f}`, and `{value!r}`. The application validates these markers and falls back for an individual translation if they are invalid.

```json
{
  "已导入 {count} 个 Mod": "Imported Mods: {count}",
  "当前项：{name}": "Current item: {name}",
  "读取失败: {e}": "Read failed: {e}"
}
```

Preserve `\n` line breaks, HTML tags, placeholders inside links, and extension patterns such as `*.zip` in file filters. Qt's `%1` and `%n` are also placeholders, and `&` can mark a button mnemonic. Count expressions such as “Mods: {count}” are recommended because they do not depend on singular or plural forms.

User content, including game, Mod, and preset names, author descriptions, and custom tags, remains unchanged. Diagnostic logs, protocol identifiers, INI keys, and file paths are not subject to word replacement. System text in native operating-system windows is supplied by the operating system.

## File locations and automatic updates

- Both source runs and release builds read languages from `languages/` inside the configuration directory, that is, `config/languages/` under the application's data root. The button in Settings opens the directory actually in use.
- Each language has one `strings.json` file that you can edit directly. There is no overlay of built-in and user packs, and no separate source-text mapping resource.
- Both standalone and onefile release packages place the default Chinese and English files in `config/languages/` beside the EXE. Include this folder when copying the onefile version as well.
- Automatic updates preserve existing language files and add only files that do not yet exist, without overwriting user edits. New text without a translation falls back to its source text until a language pack author adds the translation.

A language pack may contain only some translations; the remaining text uses the language specified by `_meta.fallback` or the source text. To change a few phrases in a default language, edit the corresponding entries in its existing file.

## Developer tools

Run these commands from the repository root:

```powershell
# Update the Chinese template from explicit translation calls, static markers, and bundled plugin manifests
python scripts/i18n/i18n_catalog.py --update

# Create a template with empty translations; an existing language pack is never overwritten
python scripts/i18n/i18n_catalog.py --new ja-JP --name 日本語

# Check JSON, duplicate keys, types, and placeholders; third-party packs may be incomplete
python scripts/i18n/i18n_catalog.py --check

# Require the selected language to cover all application source text before release
python scripts/i18n/i18n_catalog.py --check --require-complete en-US
```

The tool scans the current source code directly to update the Chinese template, create language packs, and check translation coverage. It does not generate a list of source locations.

The source text is the unique translation key: code calls `t("设置")`, and a language pack provides `"设置": "Settings"` in `messages`. Language pack authors only need to edit each language's `strings.json` under `config/languages/`.

Use `t("原文")` for new static text. For text with parameters, use `t("已导入 {count} 个 Mod", count=count)`; do not insert dynamic values before translation. Mark module constants and default arguments with `N_("原文")`, then call `t()` when displaying them. Keep API data, enum values, and persisted settings separate from display text. Lists should store the underlying values in `itemData`.

Source text passed to the interface through variables or callbacks also needs an `N_()` marker at its definition. Lower-level modules that should not load application configuration can import this pure marker function from `src.utils.language_pack`; the extraction tool recognizes it there as well.

Language packs are read as data. Translation does not scan windows, modify source documents, or automatically rewrite users' language packs.
