"""Docs pages from the user's myweb (5docs + 5docsView),
adapted for the hub: API base derived from the page URL, vendored
statics made relative, auth gate injected."""

PAGES = {
    'docs': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta
        name="viewport"
        content="width=device-width, initial-scale=1.0, viewport-fit=cover"
    >

    <title>知识笔记 · 个人知识工作台</title>

    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <link rel="stylesheet" href="static/cdn/element-plus2.14.3.css">
    <script src="static/cdn/element-plus2.14.3.js"></script>
    <script src="static/cdn/element.vue.js"></script>
    <script src="static/cdn/tailwindcss3.4.17.js"></script>

    <link rel="stylesheet" href="static/cdn/vditor.css">
    <script src="static/cdn/vditor.js"></script>

    <style>
        * {
            box-sizing: border-box;
        }

        html,
        body,
        #app {
            width: 100%;
            height: 100%;
            margin: 0;
        }

        body {
            overflow: hidden;
            background: #f8fafc;
        }

        ::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }

        ::-webkit-scrollbar-thumb {
            background: #cbd5e1;
            border-radius: 3px;
        }

        ::-webkit-scrollbar-track {
            background: transparent;
        }

        .highlight-mark {
            background: #fef08a;
            color: #b91c1c;
            border-radius: 2px;
            padding: 0 2px;
        }

        ::highlight(search-results) {
            background: #fef08a;
            color: #b91c1c;
            font-weight: bold;
        }

        /* =====================================================
           Vditor
           ===================================================== */

        .vditor {
            border: none !important;
        }

        .vditor-toolbar {
            border-bottom: 1px solid #f1f5f9 !important;
            background: #f8fafc !important;
            padding-left: 20px !important;
        }

        .vditor-ir,
        .vditor-wysiwyg {
            padding: 20px 40px !important;
        }

        /* =====================================================
           侧栏
           ===================================================== */

        .document-aside {
            width: 320px;
            min-width: 320px;
            max-width: 320px;
        }

        .timeline-aside {
            width: 430px;
            min-width: 430px;
            max-width: 430px;
        }

        /* =====================================================
           时间线
           ===================================================== */

        .timeline-panel {
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            background: #f8fafc;
        }

        .timeline-header {
            flex-shrink: 0;
            background: #ffffff;
            border-bottom: 1px solid #e5e7eb;
        }

        .timeline-header-main {
            min-height: 47px;
            padding: 7px 10px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
        }

        .timeline-main-title {
            min-width: 0;
            font-size: 14px;
            line-height: 1.3;
            color: #334155;
            font-weight: 700;
        }

        .timeline-main-count {
            margin-top: 2px;
            font-size: 10px;
            color: #94a3b8;
        }

        .timeline-options {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 0 10px 7px;
        }

        /* =====================================================
           月历
           ===================================================== */

        .calendar-container {
            flex-shrink: 0;
            background: #ffffff;
            border-bottom: 1px solid #e5e7eb;
            padding: 7px 9px 8px;
        }

        .calendar-toolbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 5px;
        }

        .calendar-month {
            font-size: 13px;
            font-weight: 700;
            color: #334155;
        }

        .calendar-grid {
            width: 100%;
            display: grid;
            grid-template-columns: repeat(7, minmax(0, 1fr));
            gap: 2px;
        }

        .calendar-weekday {
            height: 17px;
            line-height: 17px;
            text-align: center;
            font-size: 9px;
            color: #94a3b8;
        }

        .calendar-cell {
            height: 30px;
            border: 1px solid transparent;
            border-radius: 5px;
            padding: 1px;
            cursor: pointer;
            user-select: none;
        }

        .calendar-cell:hover {
            background: #f8fafc;
        }

        .calendar-cell.other-month {
            opacity: 0.35;
        }

        .calendar-cell.today {
            border-color: #93c5fd;
        }

        .calendar-cell.selected {
            background: #eff6ff;
            border-color: #3b82f6;
        }

        .calendar-number {
            height: 15px;
            line-height: 15px;
            text-align: center;
            font-size: 10px;
            color: #475569;
        }

        .calendar-cell.selected .calendar-number {
            color: #2563eb;
            font-weight: 700;
        }

        .calendar-count {
            height: 10px;
            line-height: 10px;
            text-align: center;
            font-size: 8px;
            color: transparent;
        }

        .calendar-cell.has-docs .calendar-count {
            color: #2563eb;
            font-weight: 700;
        }

        .calendar-cell.heavy .calendar-count {
            color: #7c3aed;
        }

        .timeline-calendar-collapsed {
            display: flex;
            align-items: center;
            justify-content: space-between;
            min-height: 37px;
            padding: 5px 10px;
            background: #ffffff;
            border-bottom: 1px solid #e5e7eb;
            flex-shrink: 0;
        }

        .collapsed-month {
            font-size: 12px;
            font-weight: 700;
            color: #475569;
        }

        .collapsed-info {
            margin-left: 8px;
            color: #94a3b8;
            font-size: 10px;
        }

        /* =====================================================
           时间线文章
           ===================================================== */

        .timeline-document-list {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            padding: 9px;
        }

        .timeline-group {
            margin-bottom: 14px;
        }

        .timeline-group-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin: 1px 2px 6px;
        }

        .timeline-group-title {
            min-width: 0;
            font-size: 12px;
            font-weight: 700;
            color: #64748b;
        }

        .timeline-group-count {
            flex-shrink: 0;
            font-size: 10px;
            color: #94a3b8;
        }

        .timeline-document {
            background: #ffffff;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 9px 10px;
            margin-bottom: 6px;
            cursor: pointer;
            transition: background 0.12s, border-color 0.12s;
        }

        .timeline-document:hover {
            background: #f8fbff;
            border-color: #93c5fd;
        }

        .timeline-document:active {
            background: #eff6ff;
        }

        .timeline-document.active {
            background: #eff6ff;
            border-color: #60a5fa;
        }

        .timeline-document-title {
            font-size: 13px;
            line-height: 1.45;
            color: #334155;
            font-weight: 600;
            word-break: break-word;
        }

        .timeline-document-meta {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            margin-top: 4px;
            font-size: 10px;
            color: #94a3b8;
        }

        .timeline-document-preview {
            margin-top: 4px;
            color: #94a3b8;
            font-size: 10px;
            line-height: 1.45;
            display: -webkit-box;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
            overflow: hidden;
        }

        .timeline-empty {
            padding: 36px 10px;
            text-align: center;
            color: #94a3b8;
            font-size: 12px;
        }

        /* =====================================================
           文档 ID
           ===================================================== */

        .doc-id-input {
            width: 210px;
        }

        .rename-hint {
            font-size: 10px;
            color: #94a3b8;
            white-space: nowrap;
        }

        /* =====================================================
           手机底部
           ===================================================== */

        .mobile-bottom-nav {
            display: none;
        }

        .mobile-nav-item {
            flex: 1;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            gap: 2px;
            color: #94a3b8;
            font-size: 11px;
            cursor: pointer;
            user-select: none;
        }

        .mobile-nav-item.active {
            color: #2563eb;
            font-weight: 600;
        }

        /* =====================================================
           移动端
           ===================================================== */

        @media (max-width: 768px) {
            header {
                height: auto !important;
                min-height: 56px;
                padding: 7px 9px !important;
                flex-wrap: wrap;
                gap: 6px;
            }

            header > div:nth-child(1) {
                flex: 1;
                min-width: 0;
            }

            header > div:nth-child(2) {
                width: 100% !important;
                max-width: none !important;
                order: 3;
            }

            header > div:nth-child(3) {
                flex-shrink: 0;
            }

            .timeline-aside,
            .document-aside {
                width: 100% !important;
                min-width: 100% !important;
                max-width: 100% !important;
            }

            .vditor-ir,
            .vditor-wysiwyg {
                padding: 12px 16px !important;
            }

            .vditor-toolbar {
                padding-left: 7px !important;
            }

            .mobile-editor-header {
                min-height: 48px !important;
                height: auto !important;
                padding: 4px 7px !important;
            }

            .mobile-save-status {
                display: none;
            }

            .doc-id-input {
                width: 115px !important;
            }

            .rename-hint {
                display: none;
            }

            .timeline-document-list {
                padding: 7px;
            }

            .calendar-container {
                padding: 6px 7px 7px;
            }

            .calendar-cell {
                height: 28px;
            }

            .mobile-bottom-nav {
                display: flex;
                height: 54px;
                flex-shrink: 0;
                background: #ffffff;
                border-top: 1px solid #e5e7eb;
                padding-bottom: env(safe-area-inset-bottom);
            }
        }

/* ===== 2026 UI refresh: visuals only; existing interaction/API preserved ===== */
:root {
  --el-color-primary: #4f46e5;
  --el-color-primary-light-3: #7771ef;
  --el-color-primary-light-5: #a19dfa;
  --el-color-primary-light-9: #f0efff;
  --el-border-radius-base: 9px;
  --ui-ink: #18233b;
  --ui-muted: #74809a;
  --ui-line: #e9edf5;
  --ui-surface: #ffffff;
  --ui-soft: #f6f8fc;
  --ui-brand: #4f46e5;
}
html, body, #app { min-height: 0; }
body { color: var(--ui-ink); background: var(--ui-soft); font-family: Inter,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif; }
button, a, [role="button"] { -webkit-tap-highlight-color: transparent; }
button:focus-visible, [role="button"]:focus-visible { outline: 2px solid #817cf5; outline-offset: 2px; }
.el-button { font-weight: 550; transition: background .18s, box-shadow .18s, transform .18s; }
.el-button:not(.is-disabled):active { transform: translateY(1px); }
.el-button--primary { box-shadow: 0 3px 9px rgba(79,70,229,.16); }
.el-input__wrapper { box-shadow: 0 0 0 1px #e3e8f2 inset !important; border-radius: 9px; }
.el-input__wrapper.is-focus { box-shadow: 0 0 0 1px var(--ui-brand) inset,0 0 0 3px #4f46e51b !important; }
.el-tag { border-radius: 6px; }
.app-header { height: 72px; min-height: 72px; gap: 18px; padding: 0 22px !important; background: rgba(255,255,255,.98); border-bottom: 1px solid var(--ui-line); box-shadow: 0 1px 0 rgba(18,29,54,.025); }
.brand-section { min-width: 296px; gap: 11px; }
.brand-icon { width: 39px; height: 39px; flex-shrink: 0; display: grid; place-items: center; border-radius: 12px; color: white; font-size: 21px; background: linear-gradient(145deg,#6b63f7,#4338ca); box-shadow: 0 6px 16px #4f46e529; }
.brand-copy { display: flex; flex-direction: column; white-space: nowrap; line-height: 1.35; }
.brand-copy strong { font-size: 15px; letter-spacing: .3px; color: #1e2740; }
.brand-copy span { font-size: 10px; color: #9da6b8; letter-spacing: .6px; }
.header-sync { margin-left: 12px; border-color: #e7eaf3; color: #69758d; }
.header-search { width: min(100%,500px); flex: 1; min-width: 140px; }
.header-search .el-input__wrapper { min-height: 39px; background: #f7f8fc; box-shadow: 0 0 0 1px transparent inset !important; }
.header-search .el-input__wrapper:hover { background: #f2f4fa; }
.search-shortcut { padding: 3px 5px; font-size: 10px; border: 1px solid #e3e7f0; background: white; color: #a0a9bd; border-radius: 5px; }
.header-actions { flex-shrink: 0; gap: 16px; }
.header-actions .el-switch__label { font-size: 12px; color: #707a91; }
.header-create { border-radius: 9px; padding-inline: 14px; }
.workspace-body { min-height: 0; }
.document-sidebar { background: #fbfcff; border-right: 1px solid var(--ui-line); box-shadow: 1px 0 0 #f7f8fc; }
.document-aside { width: 294px; min-width: 294px; max-width: 294px; }
.timeline-aside { width: 382px; min-width: 382px; max-width: 382px; }
.sidebar-top { padding: 19px 13px 14px; background: #fff; border-bottom: 1px solid var(--ui-line); }
.side-heading { display:flex; align-items:center; justify-content:space-between; padding:0 8px 15px; font-size:12px; font-weight:700; letter-spacing:.5px; color:#58647c; }
.total-pill { font-weight:600; background:#f3f5fa; color:#8893a9; padding:4px 8px; border-radius:20px; font-size:10px; }
.mode-switch { display: grid; grid-template-columns: 1fr 1fr; padding: 4px; gap: 4px; border-radius: 11px; background: #f1f3f9; }
.mode-button { min-height:34px; display:flex; justify-content:center; align-items:center; gap:7px; border:0; border-radius:8px; cursor:pointer; background:transparent; color:#8a94a8; font-size:12px; font-weight:650; transition:all .16s; }
.mode-button:hover { color:#4f46e5; }
.mode-button.active { color:#4f46e5; background:white; box-shadow:0 2px 6px #2e346214; }
.mode-arrow { font-size:10px; }
.quick-create { width: 100%; margin-top: 13px; height: 37px; border-radius:9px; }
.create-key { margin-left:auto; color:#ffffffbd; font-size:9px; font-weight:400; }
.document-sidebar > div:nth-child(2) { padding: 10px 9px 26px; }
.section-label { display:flex; justify-content:space-between; align-items:center; gap:8px; margin: 16px 8px 8px; font-size:11px; font-weight:700; color:#78849b; }
.section-label:first-child { margin-top: 8px; }
.section-label > span:first-child { display:flex; align-items:center; gap:6px; }
.section-label .el-icon { font-size:12px; }
.pinned-label { color:#c58433; }
.counter { color:#9ba5b7; border-radius:10px; min-width:19px; text-align:center; background:#eff1f7; padding:2px 5px; font-weight:500; font-size:10px; }
.doc-row { display:flex; align-items:center; min-height:35px; padding: 9px 11px; margin:2px 3px; border: 1px solid transparent; border-radius:9px; font-size:12px; line-height:1.4; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; cursor:pointer; color:#455069; transition:all .15s; }
.doc-row:hover { background:#f2f3fa; color:#4b42c8; }
.doc-row.bg-blue-100, .doc-row.bg-orange-100, .doc-row.bg-slate-200 { background:#efeeff !important; color:#453bc3 !important; border-color:#e1dfff; font-weight:700; }
.doc-row.text-slate-500 { color:#94a0b1; }
.highlight-mark { background:#fff2aa; color:#915300; border-radius:3px; padding:1px 2px; }
.empty-small { padding:28px 12px; color:#99a4b5; display:flex; flex-direction:column; align-items:center; gap:7px; text-align:center; font-size:12px; }
.empty-small .el-icon { font-size:27px; color:#bfc6d7; }
.empty-small small { color:#adb5c4; font-size:10px; }
.editor-main { background:#fff; }
.editor-empty { background: radial-gradient(ellipse at 50% 30%, #f3f2ff 0%, #ffffff 58%); padding:30px; }
.empty-hero { display:flex; flex-direction:column; align-items:center; max-width:550px; text-align:center; }
.empty-hero-icon { width:76px; height:76px; border-radius:23px; display:grid; place-items:center; font-size:32px; background:#edebff; color:var(--ui-brand); box-shadow:0 12px 42px #7771ef18; margin-bottom:24px; }
.empty-hero h1 { font-size:24px; letter-spacing:.6px; font-weight:750; color:#27324b; margin:0 0 12px; }
.empty-hero p { font-size:13px; line-height:1.8; color:#919caf; margin:0 0 26px; }
.empty-hero .el-button { padding:18px 25px; font-size:13px; }
.empty-shortcut { font-size:11px; color:#acb5c5; margin-top:30px; }
.editor-toolbar { background:#fff !important; border-color:var(--ui-line) !important; min-height:58px; height:58px; gap:12px; }
.editor-toolbar .doc-id-input { width:225px; }
.editor-toolbar .el-input-group__prepend { font-size:10px; color:#7c87a0; background:#f5f6fa; border:0; }
.editor-toolbar .rename-hint { color:#aab2c3; font-size:10px; }
.editor-toolbar .el-button--success { --el-button-bg-color: #4f46e5; --el-button-border-color: #4f46e5; --el-button-hover-bg-color: #6259e6; --el-button-hover-border-color: #6259e6; --el-button-active-bg-color: #4338ca; --el-button-active-border-color: #4338ca; }
.editor-canvas { background:#ffffff; }
.vditor { border:none !important; }
.vditor-toolbar { padding:7px 14px !important; min-height:44px; background:#fff !important; border-bottom:1px solid var(--ui-line) !important; }
.vditor-toolbar__item { border-radius:6px; }
.vditor-ir,.vditor-wysiwyg { padding:25px clamp(20px,5vw,62px) !important; }
.vditor-ir .vditor-reset, .vditor-wysiwyg .vditor-reset { max-width:920px; margin:0 auto; color:#2f3b50; font-size:15px; line-height:1.85; }
.editor-statusbar { height:34px; flex-shrink:0; display:flex; align-items:center; gap:14px; padding:0 22px; background:#fbfcff; border-top:1px solid var(--ui-line); color:#a2abbb; font-size:10px; }
.status-indicator { width:6px; height:6px; border-radius:50%; background:#65b895; box-shadow:0 0 0 3px #65b89517; }
.status-indicator.status-dirty { background:#eca946; box-shadow:0 0 0 3px #eca94617; }
.status-indicator.status-error { background:#e86666; box-shadow:0 0 0 3px #e8666617; }
.status-text { color:#718099; }
.statusbar-spacer { flex:1; }
.inspector-aside { width:284px; min-width:284px; max-width:284px; background:#fafbfe; border-color:var(--ui-line); padding:18px 15px; gap:13px; }
.inspector-aside > div { border-radius:12px !important; border:1px solid #eaedf5 !important; box-shadow:0 4px 15px #1b274006 !important; padding:16px !important; }
.inspector-aside h3 { font-size:12px; }
.inspector-aside .el-tag { max-width:100%; }
.inspector-aside .related-links > div { padding:7px 8px; border-radius:6px; }
.inspector-aside .related-links > div:hover { background:#f1f0ff; }
.generated-tags { display:flex; flex-wrap:wrap; gap:6px; margin-top:12px; }
.timeline-panel { background:#fafbfe; }
.timeline-header { border-color:var(--ui-line); padding:7px 5px; }
.timeline-main-title { color:#28334d; font-size:14px; }
.timeline-options { padding-top:6px; }
.calendar-container { padding:10px 12px 13px; border-color:var(--ui-line); }
.calendar-month { color:#4d5673; }
.calendar-grid { gap:3px; }
.calendar-weekday { height:22px; line-height:22px; font-size:10px; }
.calendar-cell { height:38px; display:flex; flex-direction:column; align-items:center; justify-content:center; border-radius:7px; }
.calendar-number { font-size:11px; height:17px; line-height:17px; }
.calendar-cell.today { border-color:#c8c5fc; }
.calendar-cell.selected { background:#efeeff; border-color:#8882ed; }
.calendar-cell.selected .calendar-number { color:#4f46e5; }
.calendar-count { height:13px; line-height:13px; font-size:9px; }
.calendar-cell.has-docs .calendar-count { color:#645de0; }
.timeline-document-list { padding:12px; }
.timeline-document { border:1px solid #eaedf5; box-shadow:0 2px 10px #1e2b4705; border-radius:10px; padding:12px 13px; margin-bottom:8px; }
.timeline-document:hover { border-color:#b4b0f4; background:#faf9ff; }
.timeline-document.active { background:#f0efff; border-color:#a9a4f2; }
.timeline-document-title { color:#2f3b53; }
.timeline-calendar-collapsed { border-color:var(--ui-line); }
.mobile-bottom-nav { z-index:15; }
.mobile-nav-item.active { color:var(--ui-brand); }
@media (min-width:769px) and (max-width:1160px) {
  .brand-section { min-width:210px; }
  .brand-copy span,.header-sync,.auto-ai-switch,.rename-hint { display:none !important; }
  .inspector-aside { width:250px; min-width:250px; max-width:250px; }
  .document-aside { width:260px; min-width:260px; max-width:260px; }
  .timeline-aside { width:345px; min-width:345px; max-width:345px; }
}
@media (max-width:768px) {
  .app-header { height:auto !important; min-height:64px; padding:10px 12px !important; gap:9px; }
  .brand-section { min-width:0; flex:1; gap:9px; }
  .brand-icon { width:33px; height:33px; font-size:18px; border-radius:10px; }
  .brand-copy strong { font-size:13px; }
  .brand-copy span,.header-sync,.auto-ai-switch { display:none !important; }
  .header-actions { gap:6px; }
  .header-search { width:100% !important; max-width:none; flex:none; order:3; }
  .header-search .el-input__wrapper { min-height:36px; }
  .document-sidebar { border:0; }
  .document-aside,.timeline-aside { width:100% !important; min-width:100% !important; max-width:100% !important; }
  .sidebar-top { padding:11px 12px 12px; }
  .side-heading { padding-bottom:9px; }
  .quick-create { height:35px; }
  .editor-toolbar { height:auto !important; min-height:57px !important; padding:8px !important; align-items:flex-start; flex-wrap:wrap; gap:7px; }
  .editor-toolbar > div:first-child { min-width:0; flex:1; flex-wrap:wrap; gap:5px; }
  .editor-toolbar > div:last-child { gap:5px; margin-left:auto; }
  .editor-toolbar .doc-id-input { width: min(40vw,175px) !important; }
  .editor-toolbar .el-button { padding:7px 9px; }
  .vditor-ir,.vditor-wysiwyg { padding:12px 16px !important; }
  .vditor-toolbar { padding:5px 7px !important; }
  .editor-statusbar { padding:0 13px; height:32px; gap:9px; font-size:9px; }
  .mobile-bottom-nav { height: calc(56px + env(safe-area-inset-bottom)); padding-bottom:env(safe-area-inset-bottom); border-color:var(--ui-line); box-shadow:0 -4px 22px #1c2a4310; }
  .mobile-nav-item { font-size:10px; gap:3px; }
  .mobile-nav-item .el-icon { font-size:19px; }
  .calendar-cell { height: min(9vw,40px); }
  .timeline-document-list { padding:10px; }
  .empty-hero h1 { font-size:20px; }
  .empty-hero p { font-size:12px; }
}
@media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition-duration:.01ms !important; animation-duration:.01ms !important; } }

    </style>

<style>
  #authGate{position:fixed;inset:0;z-index:9999;background:rgba(10,12,16,.93);display:none;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  #authGate.show{display:flex}
  #authGate .card{width:min(92vw,340px);background:#161a22;border:1px solid #232a37;border-radius:14px;padding:22px 20px;display:flex;flex-direction:column;gap:10px;color:#e6e8ee}
  #authGate h2{margin:0 0 4px;font-size:16px}
  #authGate p{margin:0;font-size:12px;color:#8b93a7}
  #authGate input{background:#0f1116;border:1px solid #232a37;color:inherit;padding:10px 12px;border-radius:10px;font:inherit;outline:none;width:100%;box-sizing:border-box}
  #authGate input:focus{border-color:#3d6ea8}
  #authGate button{background:#2f6fd0;color:#fff;border:0;border-radius:10px;padding:10px;font:inherit;cursor:pointer}
  #authGate .err{color:#ff8b8b;font-size:12px;min-height:15px}
</style>
</head>

<body>

<div id="app" class="h-full flex flex-col">

    <!-- =========================================================
         HEADER
         ========================================================= -->
    <header v-show="!isFocusMode" class="app-header bg-white border-b border-slate-200 flex items-center justify-between px-4 shrink-0 z-10">
        <div class="brand-section flex items-center gap-3 min-w-0">
            <div class="brand-icon" aria-hidden="true"><el-icon><collection></collection></el-icon></div>
            <div class="brand-copy">
                <strong>知识笔记</strong>
                <span>把想法沉淀为知识</span>
            </div>
            <el-button class="header-sync" size="small" plain :loading="syncing" @click="rebuildIndex">
                <el-icon class="mr-1"><refresh></refresh></el-icon>同步索引
            </el-button>
        </div>
        <div class="header-search">
            <el-input v-model="searchQuery" placeholder="搜索标题、标签或正文（支持 tag:）" clearable
                      @keyup.enter="forceSearch" aria-label="搜索笔记">
                <template #prefix><el-icon><search></search></el-icon></template>
                <template #suffix><span v-if="!isMobile" class="search-shortcut">⌘ K</span></template>
            </el-input>
        </div>
        <div class="header-actions flex items-center gap-2">
            <el-switch v-model="autoAiTags" active-text="静默 AI 补全" size="small" class="auto-ai-switch"></el-switch>
            <el-button v-if="isMobile" type="primary" circle @click="createNew" aria-label="新建笔记">
                <el-icon><plus></plus></el-icon>
            </el-button>
            <el-button v-else type="primary" class="header-create" @click="createNew">
                <el-icon class="mr-1"><plus></plus></el-icon>新建笔记
            </el-button>
        </div>
    </header>


    <!-- =========================================================
         主体
         ========================================================= -->
    <div class="workspace-body flex-1 flex overflow-hidden">

        <!-- =====================================================
             左侧
             ===================================================== -->
        <aside
            v-show="!isFocusMode && (!isMobile || mobileView === 'list')"
            class="document-sidebar bg-slate-50 border-r border-slate-200 flex flex-col shrink-0"
            :class="sidebarMode === 'timeline'
                ? 'timeline-aside'
                : 'document-aside'">

            <!-- =================================================
                 顶部模式
                 ================================================= -->
            <div class="sidebar-top">
                <div class="side-heading"><span>我的工作区</span><span class="total-pill">{{ documents.length }} 篇</span></div>
                <div class="mode-switch">
                    <button type="button" class="mode-button" :class="{ active: sidebarMode === 'docs' }"
                            @click="sidebarMode = 'docs'">
                        <el-icon><document></document></el-icon>文档
                    </button>
                    <button type="button" class="mode-button" :class="{ active: sidebarMode === 'timeline' }"
                            @click="toggleTimelinePanel">
                        <el-icon><calendar></calendar></el-icon>时间线
                        <span v-if="sidebarMode === 'timeline'" class="mode-arrow">{{ calendarCollapsed ? '▾' : '▴' }}</span>
                    </button>
                </div>
                <el-button v-if="sidebarMode === 'docs'" type="primary" class="quick-create" @click="createNew">
                    <el-icon class="mr-1"><edit-pen></edit-pen></el-icon>快速记录
                    <span class="create-key">Ctrl N</span>
                </el-button>
            </div>

            <!-- =================================================
                 文档列表
                 ================================================= -->
            <div
                v-if="sidebarMode === 'docs'"
                class="flex-1 overflow-y-auto p-2">

                <template v-if="searchQuery.trim()">

                    <div class="section-label search-result-label">
                        <span>搜索结果</span><span class="counter">{{ searchResults.length }}</span>
                    </div>
                    <div v-if="!isSearching && searchResults.length === 0" class="empty-small">
                        <el-icon><search></search></el-icon>
                        <span>没有找到匹配的笔记</span>
                        <small>尝试换个关键词或使用 tag:标签</small>
                    </div>

                    <div
                        v-for="doc in searchResults"
                        :key="doc.id"
                        @click="loadDocument(doc.id)" :title="getDocTitle(doc)" tabindex="0" role="button" @keyup.enter="loadDocument(doc.id)"
                        :class="[
                            'doc-row search-doc-row',
                            activeDocId === doc.id
                                ? 'bg-blue-100 text-blue-700 font-medium'
                                : 'hover:bg-slate-200 text-slate-700'
                        ]"
                        v-html="highlightText(getDocTitle(doc))">
                    </div>

                </template>


                <template v-else>

                    <div v-if="pinnedDocs.length > 0">

                        <div class="section-label pinned-label"><span><el-icon><star-filled></star-filled></el-icon> 置顶</span><span class="counter">{{ pinnedDocs.length }}</span></div>

                        <div
                            v-for="doc in pinnedDocs"
                            :key="doc.id"
                            @click="loadDocument(doc.id)" :title="getDocTitle(doc)" tabindex="0" role="button" @keyup.enter="loadDocument(doc.id)"
                            :class="[
                                'doc-row',
                                activeDocId === doc.id
                                    ? 'bg-orange-100 text-orange-700 font-medium'
                                    : 'text-slate-700 hover:bg-slate-200'
                            ]">

                            {{ getDocTitle(doc) }}

                        </div>

                    </div>


                    <div class="section-label"><span><el-icon><document></document></el-icon> 收件箱</span><span class="counter">{{ inboxDocs.length }}</span></div>

                    <div
                        v-for="doc in inboxDocs"
                        :key="doc.id"
                        @click="loadDocument(doc.id)" :title="getDocTitle(doc)" tabindex="0" role="button" @keyup.enter="loadDocument(doc.id)"
                        :class="[
                            'doc-row',
                            activeDocId === doc.id
                                ? 'bg-blue-100 text-blue-700 font-medium'
                                : 'text-slate-700 hover:bg-slate-200'
                        ]">

                        {{ getDocTitle(doc) }}

                    </div>


                    <div class="section-label"><span><el-icon><folder></folder></el-icon> 归档</span><span class="counter">{{ archiveDocs.length }}</span></div>

                    <div
                        v-for="doc in archiveDocs"
                        :key="doc.id"
                        @click="loadDocument(doc.id)" :title="getDocTitle(doc)" tabindex="0" role="button" @keyup.enter="loadDocument(doc.id)"
                        :class="[
                            'doc-row',
                            activeDocId === doc.id
                                ? 'bg-slate-200 text-slate-800 font-medium'
                                : 'text-slate-500 hover:bg-slate-200'
                        ]">

                        {{ getDocTitle(doc) }}

                    </div>
                    <div v-if="documents.length === 0" class="empty-small"><el-icon><document></document></el-icon><span>还没有笔记</span><small>点击“快速记录”开始</small></div>
                </template>

            </div>


            <!-- =================================================
                 时间线
                 ================================================= -->
            <div
                v-else
                class="timeline-panel">

                <!-- 时间线头部 -->

                <div class="timeline-header">

                    <div class="timeline-header-main">

                        <div class="min-w-0">

                            <div class="timeline-main-title">
                                {{ timelineTitle }}
                            </div>

                            <div class="timeline-main-count">
                                {{ timelineDocs.length }} 篇
                            </div>

                        </div>


                        <div class="flex items-center gap-1">

                            <el-button
                                size="small"
                                circle
                                @click="moveTimelineMonth(-1)">

                                <el-icon>
                                    <arrow-left></arrow-left>
                                </el-icon>

                            </el-button>


                            <el-button
                                size="small"
                                @click="timelineToday">

                                今天

                            </el-button>


                            <el-button
                                size="small"
                                circle
                                @click="moveTimelineMonth(1)">

                                <el-icon>
                                    <arrow-right></arrow-right>
                                </el-icon>

                            </el-button>

                        </div>

                    </div>


                    <div class="timeline-options">

                        <el-radio-group
                            v-model="timelineTimeField"
                            size="small">

                            <el-radio-button label="created">
                                创建
                            </el-radio-button>

                            <el-radio-button label="updated">
                                修改
                            </el-radio-button>

                        </el-radio-group>


                        <el-radio-group
                            v-model="timelineRangeMode"
                            size="small">

                            <el-radio-button label="day">
                                日
                            </el-radio-button>

                            <el-radio-button label="week">
                                周
                            </el-radio-button>

                        </el-radio-group>

                    </div>

                </div>


                <!-- =================================================
                     展开日历
                     ================================================= -->
                <div
                    v-show="!calendarCollapsed"
                    class="calendar-container">

                    <div class="calendar-toolbar">

                        <span class="calendar-month">
                            {{ calendarTitle }}
                        </span>

                        <div class="flex items-center gap-1">

                            <el-button
                                link
                                size="small"
                                @click="moveTimelineMonth(-1)">
                                ‹
                            </el-button>

                            <el-button
                                link
                                size="small"
                                @click="timelineToday">
                                今天
                            </el-button>

                            <el-button
                                link
                                size="small"
                                @click="moveTimelineMonth(1)">
                                ›
                            </el-button>

                        </div>

                    </div>


                    <div class="calendar-grid">

                        <div
                            v-for="day in weekNames"
                            :key="day"
                            class="calendar-weekday">

                            {{ day }}

                        </div>


                        <div
                            v-for="cell in calendarCells"
                            :key="cell.key"
                            class="calendar-cell"
                            :class="{
                                'other-month': !cell.currentMonth,
                                'today': cell.today,
                                'selected': cell.selected,
                                'has-docs': cell.count > 0,
                                'heavy': cell.count >= 5
                            }"
                            @click="selectCalendarDay(cell.date)">

                            <div class="calendar-number">
                                {{ cell.day }}
                            </div>

                            <div class="calendar-count">
                                <span v-if="cell.count > 0">
                                    {{ cell.count }}
                                </span>
                            </div>

                        </div>

                    </div>

                </div>


                <!-- =================================================
                     折叠日历
                     ================================================= -->
                <div
                    v-show="calendarCollapsed"
                    class="timeline-calendar-collapsed">

                    <div class="flex items-center min-w-0">

                        <span class="collapsed-month">
                            {{ calendarTitle }}
                        </span>

                        <span class="collapsed-info">
                            本月 {{ currentMonthDocCount }} 篇
                        </span>

                    </div>

                    <span class="text-[10px] text-slate-400">
                        再点“时间线”展开
                    </span>

                </div>


                <!-- =================================================
                     文档时间线
                     ================================================= -->
                <div class="timeline-document-list">

                    <!-- 日 -->

                    <template v-if="timelineRangeMode === 'day'">

                        <div class="timeline-group">

                            <div class="timeline-group-header">

                                <span class="timeline-group-title">
                                    {{ selectedDateLabel }}
                                </span>

                                <span class="timeline-group-count">
                                    {{ timelineDocs.length }} 篇
                                </span>

                            </div>


                            <div
                                v-for="doc in timelineDocs"
                                :key="doc.id"
                                class="timeline-document"
                                :class="{
                                    active: activeDocId === doc.id
                                }"
                                @click="loadDocumentFromTimeline(doc.id)">

                                <div class="timeline-document-title">
                                    {{ getDocTitle(doc) }}
                                </div>

                                <div class="timeline-document-meta">

                                    <span>
                                        {{ formatTimelineTime(doc) }}
                                    </span>

                                    <span>

                                        <span v-if="doc.is_pinned === 1">
                                            📌
                                        </span>

                                        <span v-if="doc.status === 'archive'">
                                            📦
                                        </span>

                                    </span>

                                </div>

                                <div
                                    v-if="getDocPreview(doc)"
                                    class="timeline-document-preview">

                                    {{ getDocPreview(doc) }}

                                </div>

                            </div>


                            <div
                                v-if="timelineDocs.length === 0"
                                class="timeline-empty">

                                这一天没有文章

                            </div>

                        </div>

                    </template>


                    <!-- 周 -->

                    <template v-else>

                        <div class="timeline-group">

                            <div class="timeline-group-header">

                                <span class="timeline-group-title">
                                    {{ selectedWeekLabel }}
                                </span>

                                <span class="timeline-group-count">
                                    {{ timelineDocs.length }} 篇
                                </span>

                            </div>


                            <div
                                v-for="group in timelineDayGroups"
                                :key="group.key">

                                <div class="timeline-group-header mt-3">

                                    <span class="timeline-group-title">
                                        {{ group.label }}
                                    </span>

                                    <span class="timeline-group-count">
                                        {{ group.docs.length }} 篇
                                    </span>

                                </div>


                                <div
                                    v-for="doc in group.docs"
                                    :key="doc.id"
                                    class="timeline-document"
                                    :class="{
                                        active: activeDocId === doc.id
                                    }"
                                    @click="loadDocumentFromTimeline(doc.id)">

                                    <div class="timeline-document-title">
                                        {{ getDocTitle(doc) }}
                                    </div>

                                    <div class="timeline-document-meta">

                                        <span>
                                            {{ formatTimelineTime(doc) }}
                                        </span>

                                        <span>

                                            <span v-if="doc.is_pinned === 1">
                                                📌
                                            </span>

                                            <span v-if="doc.status === 'archive'">
                                                📦
                                            </span>

                                        </span>

                                    </div>

                                </div>

                            </div>


                            <div
                                v-if="timelineDocs.length === 0"
                                class="timeline-empty">

                                这一周没有文章

                            </div>

                        </div>

                    </template>

                </div>

            </div>

        </aside>


        <!-- =====================================================
             主编辑区
             ===================================================== -->

        <main
            v-show="!isMobile || mobileView === 'editor'"
            class="editor-main flex-1 flex flex-col bg-white relative min-w-0">

            <!-- 空状态 -->

            <div v-show="!activeDocId && !isCreating" class="editor-empty absolute inset-0 flex items-center justify-center">
                <div class="empty-hero">
                    <div class="empty-hero-icon"><el-icon><edit-pen></edit-pen></el-icon></div>
                    <h1>让每一个想法，都有归处</h1>
                    <p>随手记录、分类整理、通过时间线回顾你的知识积累。</p>
                    <el-button type="primary" size="large" @click="createNew"><el-icon class="mr-1"><plus></plus></el-icon>写下第一条笔记</el-button>
                    <div class="empty-shortcut">支持 Markdown 编辑 · 自动保存 · 全文搜索</div>
                </div>
            </div>

            <!-- 编辑器 -->

            <div
                v-show="activeDocId || isCreating"
                class="flex flex-col h-full w-full">

                <div
                    class="editor-toolbar h-12 border-b border-slate-100 flex items-center justify-between px-4 shrink-0 mobile-editor-header">

                    <div class="flex items-center gap-2 min-w-0">

                        <el-button
                            v-if="isMobile"
                            size="small"
                            circle
                            plain
                            @click="flushAndBack">

                            <el-icon>
                                <arrow-left></arrow-left>
                            </el-icon>

                        </el-button>


                        <el-button
                            size="small"
                            circle
                            plain
                            @click="isFocusMode = !isFocusMode">

                            <el-icon>
                                <full-screen></full-screen>
                            </el-icon>

                        </el-button>


                        <!-- 文件名 -->

                        <el-input
                            v-model="docIdInput"
                            class="doc-id-input"
                            size="small"
                            :disabled="isCreating || renaming"
                            @keyup.enter="commitRename"
                            @blur="commitRename">

                            <template #prepend>
                                ID
                            </template>

                        </el-input>


                        <el-button
                            size="small"
                            plain
                            :loading="clipboardTitleLoading"
                            :disabled="!activeDocId && !isCreating"
                            @click="setTitleFromClipboard">

                            <el-icon class="mr-1"><magic-stick></magic-stick></el-icon>提取标题

                        </el-button>


                        <span class="rename-hint">回车或失焦重命名</span>

                    </div>


                    <div class="flex items-center gap-3 shrink-0">

                        <!-- 【新增】移动端删除按钮 -->
                        <el-button
                            v-if="isMobile && activeDocId"
                            size="small"
                            type="danger"
                            plain
                            circle
                            :loading="deleting"
                            @click="deleteDocument">

                            <el-icon>
                                <delete></delete>
                            </el-icon>

                        </el-button>


                        <span
                            class="text-xs mobile-save-status"
                            :class="{
                                'text-red-500':
                                    saveStatus === '保存失败',

                                'text-blue-500':
                                    saveStatus === '保存中...',

                                'text-orange-500':
                                    saveStatus === '有未保存修改',

                                'text-slate-400':
                                    ![
                                        '保存失败',
                                        '保存中...',
                                        '有未保存修改'
                                    ].includes(saveStatus)
                            }">

                            {{ saveStatus }}

                        </span>


                        <el-button
                            size="small"
                            type="success"
                            @click="manualSave"
                            :loading="saving">

                            <el-icon class="mr-1"><check></check></el-icon>保存

                        </el-button>

                    </div>

                </div>


                <div class="flex-1 overflow-hidden relative editor-canvas">
                    <div id="vditor-container" class="absolute inset-0"></div>
                </div>
                <div class="editor-statusbar" aria-live="polite">
                    <span class="status-indicator" :class="{ 'status-error': saveStatus === '保存失败', 'status-dirty': dirty }"></span>
                    <span class="status-text">{{ saveStatus || '就绪' }}</span>
                    <span class="statusbar-spacer"></span>
                    <span>{{ charCount }} 字</span><span>{{ lineCount }} 行</span><span>约 {{ readTime }} 分钟阅读</span>
                </div>
            </div>

        </main>


        <!-- =====================================================
             右侧信息
             ===================================================== -->

        <aside
            v-show="(activeDocId || isCreating) && !isFocusMode && !isMobile"
            class="inspector-aside w-72 bg-slate-50 border-l border-slate-200 p-4 flex flex-col gap-4 overflow-y-auto shrink-0">

            <!-- 【修复】操作区：置顶 / 归档 / 删除 -->

            <div
                v-if="activeDocId"
                class="bg-white p-3 rounded-lg shadow-sm border border-slate-200">

                <div class="flex gap-2">

                    <el-button
                        size="small"
                        class="flex-1"
                        :type="activeDocMeta.is_pinned ? 'warning' : 'default'"
                        @click="toggleAction('toggle_pin')">

                        {{ activeDocMeta.is_pinned
                            ? '取消置顶'
                            : '📌 置顶' }}

                    </el-button>


                    <el-button
                        size="small"
                        class="flex-1"
                        plain
                        :type="activeDocStatus === 'archive'
                            ? 'primary'
                            : 'info'"
                        @click="toggleAction('toggle_archive')">

                        {{ activeDocStatus === 'archive'
                            ? '还原活跃'
                            : '📦 归档' }}

                    </el-button>

                </div>


                <!-- 【新增】删除按钮 -->

                <el-button
                    size="small"
                    type="danger"
                    plain
                    class="w-full mt-2"
                    :loading="deleting"
                    @click="deleteDocument">

                    <el-icon class="mr-1"><delete></delete></el-icon>删除笔记

                </el-button>

            </div>


            <!-- 状态 -->

            <div class="bg-white p-4 rounded-lg shadow-sm border border-slate-200">

                <h3 class="text-sm font-bold text-slate-700 mb-3">
                    文档状态
                </h3>

                <div class="text-[13px] text-slate-600 space-y-3">

                    <div class="flex justify-between border-b border-slate-50 pb-2">

                        <span class="text-slate-400">
                            生命周期
                        </span>

                        <el-tag
                            :type="activeDocStatus === 'archive'
                                ? 'info'
                                : 'success'"
                            size="small"
                            effect="dark">

                            {{ activeDocStatus.toUpperCase() }}

                        </el-tag>

                    </div>


                    <div class="flex justify-between border-b border-slate-50 pb-2">

                        <span class="text-slate-400">
                            创建时间
                        </span>

                        <span :title="fullCreatedTime">
                            {{ createdTimeAgo }}
                        </span>

                    </div>


                    <div class="flex justify-between border-b border-slate-50 pb-2">

                        <span class="text-slate-400">
                            最后更新
                        </span>

                        <span :title="fullUpdateTime">
                            {{ updatedTimeAgo }}
                        </span>

                    </div>


                    <div class="flex justify-between border-b border-slate-50 pb-2">

                        <span class="text-slate-400">
                            字数
                        </span>

                        <span>
                            {{ charCount }} 字
                        </span>

                    </div>


                    <div class="flex justify-between border-b border-slate-50 pb-2">

                        <span class="text-slate-400">
                            总字符
                        </span>

                        <span>
                            {{ totalChars }}
                        </span>

                    </div>


                    <div class="flex justify-between border-b border-slate-50 pb-2">

                        <span class="text-slate-400">
                            行数
                        </span>

                        <span>
                            {{ lineCount }}
                        </span>

                    </div>


                    <div class="flex justify-between">

                        <span class="text-slate-400">
                            预计阅读
                        </span>

                        <span>
                            {{ readTime }} 分钟
                        </span>

                    </div>

                </div>

            </div>


            <!-- AI -->

            <div class="bg-white p-4 rounded-lg shadow-sm border border-slate-200">

                <h3 class="text-sm font-bold text-slate-700 mb-3 flex justify-between items-center">

                    <span>
                        <el-icon class="mr-1"><magic-stick></magic-stick></el-icon>AI 标签提取
                    </span>

                    <el-button
                        size="small"
                        type="primary"
                        :loading="aiLoading"
                        @click="generateTags">

                        提取

                    </el-button>

                </h3>

                <div class="text-[11px] text-slate-400">使用 AI 阅读正文并提炼 3–5 个关键词</div>
                <div v-if="generatedTags.length" class="generated-tags">
                    <el-tag v-for="tag in generatedTags" :key="tag" size="small" effect="plain">{{ tag }}</el-tag>
                    <el-button size="small" text type="primary" @click="applyGeneratedTags">应用到笔记</el-button>
                </div>


                <div
                    v-if="relatedDocs.length > 0"
                    class="mt-4 border-t border-slate-100 pt-3">

                    <h4 class="text-xs font-bold text-slate-600 mb-2">
                        🔗 相关关联推荐
                    </h4>

                    <div class="space-y-1.5 related-links">

                        <div
                            v-for="rd in relatedDocs"
                            :key="rd.id"
                            @click="loadDocument(rd.id)"
                            class="text-[12px] text-blue-600 cursor-pointer truncate">

                            {{ rd.title }}

                            <span class="text-slate-400 text-[10px]">
                                ({{ rd.overlap }} 标签重合)
                            </span>

                        </div>

                    </div>

                </div>

            </div>

        </aside>

    </div>


    <!-- =========================================================
         移动端底部
         ========================================================= -->

    <div
        v-if="isMobile && !isFocusMode"
        class="mobile-bottom-nav" role="navigation" aria-label="移动端导航">

        <div
            class="mobile-nav-item"
            :class="{
                active:
                    mobileView === 'list' &&
                    sidebarMode === 'docs'
            }"
            @click="
                mobileView = 'list';
                sidebarMode = 'docs'
            ">

            <el-icon>
                <document></document>
            </el-icon>

            <span>
                文档
            </span>

        </div>


        <div
            class="mobile-nav-item"
            :class="{
                active:
                    mobileView === 'list' &&
                    sidebarMode === 'timeline'
            }"
            @click="
                mobileView = 'list';
                sidebarMode = 'timeline'
            ">

            <el-icon>
                <calendar></calendar>
            </el-icon>

            <span>
                时间线
            </span>

        </div>


        <div
            class="mobile-nav-item"
            @click="createNew">

            <el-icon>
                <edit-pen></edit-pen>
            </el-icon>

            <span>
                记录
            </span>

        </div>

    </div>

</div>


<script>
const { createApp, ref, computed, watch, nextTick, onMounted, onUnmounted } = Vue;
const app = createApp({
    setup() {
        const documents = ref([]);
        const searchResults = ref([]);
        const searchQuery = ref('');
        const activeDocId = ref('');
        const docIdInput = ref('');
        const editorContent = ref('');
        const isCreating = ref(false);
        const activeDocMeta = ref({
            id: '',
            title: '',
            created_time: null,
            update_time: null,
            is_pinned: 0,
            status: 'active'
        });
        const relatedDocs = ref([]);
        const saving = ref(false);
        const syncing = ref(false);
        const isSearching = ref(false);
        const renaming = ref(false);
        const clipboardTitleLoading = ref(false);
        const deleting = ref(false);
        const saveStatus = ref('');
        const aiLoading = ref(false);
        const generatedTags = ref([]);
        const isFocusMode = ref(false);
        const autoAiTags = ref(localStorage.getItem('autoAiTags') === 'true');
        watch(autoAiTags, value => {
            localStorage.setItem('autoAiTags', value);
            if (value)
                scheduleAutoAiTags();
            else
                clearTimeout(autoAiTimer);
        });
        const dirty = ref(false);
        let saveTimer = null;
        let saveSequence = 0;
        const pendingSnapshot = ref(null);
        let saveQueue = Promise.resolve();
        const isMobile = ref(window.innerWidth <= 768);
        const mobileView = ref('list');
        const updateIsMobile = () => {
            isMobile.value =
                window.innerWidth <= 768;
        };
        window.addEventListener('resize', updateIsMobile);
        const sidebarMode = ref('docs');
        const timelineCursor = ref(new Date());
        const timelineTimeField = ref('created');
        const timelineRangeMode = ref('day');
        const calendarCollapsed = ref(false);
        const weekNames = [
            '一',
            '二',
            '三',
            '四',
            '五',
            '六',
            '日'
        ];
        let vditorInstance = null;
        let isVditorReady = false;
        let searchTimer = null;
        let autoAiTimer = null;
        let autoAiRunning = false;
        const scheduleAutoAiTags = (delay = 5000) => {
            clearTimeout(autoAiTimer);
            if (!autoAiTags.value || autoAiRunning)
                return;
            autoAiTimer = setTimeout(async () => {
                if (!autoAiTags.value || autoAiRunning)
                    return;
                autoAiRunning = true;
                try {
                    const response = await fetch(API_BASE + '/docapi/ai/batch_tags', { method: 'POST' });
                    if (!response.ok)
                        throw new Error(`HTTP ${response.status}`);
                    const result = await response.json();
                    if (result.processed > 0) {
                        await fetchDocuments();
                        if (activeDocId.value) {
                            const item = documents.value.find(d => d.id === activeDocId.value);
                            if (item)
                                activeDocMeta.value = { ...activeDocMeta.value, tags: item.tags, tags_cache: item.tags_cache };
                            fetchRelated(activeDocId.value);
                        }
                    }
                }
                catch (error) {
                    console.warn('后台 AI 补全未完成：', error);
                }
                finally {
                    autoAiRunning = false;
                }
            }, delay);
        };
        const getTimelineTimestamp = doc => {
            if (timelineTimeField.value ===
                'updated') {
                return Number(doc.update_time || 0);
            }
            return Number(doc.created_time || 0);
        };
        const dateKey = date => {
            const d = new Date(date);
            return [
                d.getFullYear(),
                String(d.getMonth() + 1).padStart(2, '0'),
                String(d.getDate()).padStart(2, '0')
            ].join('-');
        };
        const startOfDay = date => {
            const d = new Date(date);
            d.setHours(0, 0, 0, 0);
            return d;
        };
        const startOfWeek = date => {
            const d = startOfDay(date);
            const day = d.getDay();
            const diff = day === 0
                ? -6
                : 1 - day;
            d.setDate(d.getDate() + diff);
            return d;
        };
        const formatTimeAgo = timestamp => {
            if (!timestamp) {
                return '-';
            }
            const seconds = Math.floor((Date.now() -
                Number(timestamp) * 1000) / 1000);
            if (seconds < 60) {
                return '刚刚';
            }
            const ranges = [
                [31536000, '年'],
                [2592000, '个月'],
                [86400, '天'],
                [3600, '小时'],
                [60, '分钟']
            ];
            for (const [limit, label] of ranges) {
                const count = Math.floor(seconds / limit);
                if (count >= 1) {
                    return `${count} ${label}前`;
                }
            }
            return '-';
        };
        const formatDateTime = timestamp => {
            if (!timestamp) {
                return '-';
            }
            return new Date(Number(timestamp) * 1000).toLocaleString('zh-CN');
        };
        const formatTimelineTime = doc => {
            const ts = getTimelineTimestamp(doc);
            if (!ts) {
                return '';
            }
            return new Date(ts * 1000).toLocaleTimeString('zh-CN', {
                hour: '2-digit',
                minute: '2-digit'
            });
        };
        const getDocTitle = doc => {
            let title = doc.title || '';
            const isDefault = !title ||
                title.trim() === '' ||
                title === '新文档标题' ||
                title.startsWith('随记_') ||
                title.startsWith('note_');
            if (isDefault &&
                doc.tags_cache &&
                doc.tags_cache !== '未分类' &&
                doc.tags_cache !== '笔记') {
                return ('[提取] ' +
                    doc.tags_cache.replace(/,/g, ' | '));
            }
            return (title ||
                doc.id ||
                '未命名笔记');
        };
        const getDocPreview = doc => {
            const text = doc.preview ||
                doc.summary ||
                doc.snippet ||
                doc.content_preview ||
                '';
            if (!text) {
                return '';
            }
            return text
                .replace(/```[\s\S]*?```/g, '')
                .replace(/[#>*_`~\[\]]/g, '')
                .replace(/\s+/g, ' ')
                .trim();
        };
        const parseTitleFromContent = content => {
            const match = content.match(/^---\n([\s\S]*?)\n---/);
            if (!match) {
                return '';
            }
            const titleMatch = match[1].match(/^title:\s*(.+)$/m);
            if (!titleMatch) {
                return '';
            }
            return titleMatch[1]
                .trim()
                .replace(/^['"]|['"]$/g, '');
        };
        const setTitleFromClipboard = async () => {
            if (!activeDocId.value &&
                !isCreating.value) {
                ElementPlus.ElMessage.warning('请先打开一个文档');
                return;
            }
            if (clipboardTitleLoading.value) {
                return;
            }
            clipboardTitleLoading.value = true;
            try {
                const source = (editorContent.value || '').slice(0, 300);
                const match = source.match(/【主题】\s*\*\*\s*([\s\S]*?)\s*\*\*/);
                const title = match
                    ? match[1]
                        .replace(/[\r\n]+/g, ' ')
                        .replace(/\s+/g, ' ')
                        .trim()
                    : '';
                if (!title) {
                    ElementPlus.ElMessage.warning('文章开头 300 字内未找到“【主题】** 标题 **”格式');
                    return;
                }
                const lines = editorContent.value.split('\n');
                const frontMatterEnd = lines.findIndex((line, index) => index > 0 &&
                    line.trim() === '---');
                if (lines.length > 0 &&
                    lines[0].trim() === '---' &&
                    frontMatterEnd > 0) {
                    const titleIndex = lines.findIndex((line, index) => index > 0 &&
                        index < frontMatterEnd &&
                        /^title\s*:/i.test(line));
                    const titleLine = `title: ${JSON.stringify(title)}`;
                    if (titleIndex >= 0) {
                        lines[titleIndex] = titleLine;
                    }
                    else {
                        lines.splice(1, 0, titleLine);
                    }
                    editorContent.value =
                        lines.join('\n');
                }
                else {
                    editorContent.value =
                        `---
title: ${JSON.stringify(title)}
---\n\n${editorContent.value}`;
                }
                activeDocMeta.value = {
                    ...activeDocMeta.value,
                    title
                };
                if (activeDocId.value) {
                    syncCurrentTitleToList(title);
                }
                if (vditorInstance && isVditorReady) {
                    vditorInstance.setValue(editorContent.value);
                }
                handleInput();
                ElementPlus.ElMessage.success(`标题已更新：${title}`);
            }
            catch (error) {
                console.error('提取标题失败:', error);
                ElementPlus.ElMessage.error(error.message ||
                    '提取标题失败');
            }
            finally {
                clipboardTitleLoading.value =
                    false;
            }
        };
        const syncCurrentTitleToList = title => {
            if (!activeDocId.value ||
                !title) {
                return;
            }
            const index = documents.value.findIndex(doc => doc.id ===
                activeDocId.value);
            if (index === -1) {
                return;
            }
            documents.value[index] = {
                ...documents.value[index],
                title
            };
        };
        const syncDocumentMetaToList = meta => {
            if (!meta || !meta.id) {
                return;
            }
            const index = documents.value.findIndex(doc => doc.id === meta.id);
            if (index >= 0) {
                documents.value[index] = {
                    ...documents.value[index],
                    ...meta
                };
            }
            else {
                documents.value.unshift({
                    ...meta
                });
            }
        };
        const pinnedDocs = computed(() => documents.value.filter(doc => doc.is_pinned === 1));
        const inboxDocs = computed(() => documents.value.filter(doc => doc.status === 'active' &&
            doc.is_pinned === 0));
        const archiveDocs = computed(() => documents.value.filter(doc => doc.status === 'archive' &&
            doc.is_pinned === 0));
        const activeDocStatus = computed(() => {
            if (!activeDocId.value) {
                return isCreating.value
                    ? 'active'
                    : '-';
            }
            return (documents.value.find(doc => doc.id ===
                activeDocId.value)?.status ||
                activeDocMeta.value.status ||
                '-');
        });
        const createdTimeAgo = computed(() => formatTimeAgo(activeDocMeta.value
            .created_time));
        const updatedTimeAgo = computed(() => formatTimeAgo(activeDocMeta.value
            .update_time));
        const fullCreatedTime = computed(() => formatDateTime(activeDocMeta.value
            .created_time));
        const fullUpdateTime = computed(() => formatDateTime(activeDocMeta.value
            .update_time));
        const contentWithoutFrontMatter = computed(() => {
            const content = editorContent.value || '';
            const match = content.match(/^---\n([\s\S]*?)\n---\n?/);
            return match
                ? content.slice(match[0].length)
                : content;
        });
        const charCount = computed(() => contentWithoutFrontMatter.value
            .replace(/\s/g, '')
            .length);
        const totalChars = computed(() => contentWithoutFrontMatter.value.length);
        const lineCount = computed(() => contentWithoutFrontMatter.value
            .split('\n')
            .length);
        const paragraphCount = computed(() => contentWithoutFrontMatter.value
            .split(/\n\s*\n/)
            .filter(p => p.trim().length > 0)
            .length);
        const readTime = computed(() => Math.max(1, Math.ceil(charCount.value / 500)));
        const escapeRegExp = str => str
            ? str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
            : '';
        const highlightText = text => {
            if (!searchQuery.value) {
                return String(text ?? '').replace(/[&<>"']/g, char => ({
                    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
                }[char]));
            }
            const regex = new RegExp(`(${escapeRegExp(searchQuery.value)})`, 'gi');
            const safe = String(text ?? '').replace(/[&<>"']/g, char => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
            }[char]));
            return safe.replace(regex, '<mark class="highlight-mark">$1</mark>');
        };
        const forceSearch = async () => {
            clearTimeout(searchTimer);
            const q = searchQuery.value.trim();
            if (!q) {
                searchResults.value = [];
                return;
            }
            isSearching.value = true;
            try {
                const res = await fetch(API_BASE + `/docapi/search?q=${encodeURIComponent(q)}`);
                if (!res.ok) {
                    throw new Error(`HTTP ${res.status}`);
                }
                const data = await res.json();
                if (q === searchQuery.value.trim()) {
                    searchResults.value = data.results || [];
                }
            }
            catch (error) {
                console.error(error);
                ElementPlus.ElMessage.error('搜索失败');
            }
            finally {
                isSearching.value = false;
            }
        };
        watch(searchQuery, () => {
            clearTimeout(searchTimer);
            searchTimer =
                setTimeout(forceSearch, 400);
        });
        const calendarMonth = computed(() => {
            const date = new Date(timelineCursor.value);
            return new Date(date.getFullYear(), date.getMonth(), 1);
        });
        const calendarTitle = computed(() => calendarMonth.value
            .toLocaleDateString('zh-CN', {
            year: 'numeric',
            month: 'long'
        }));
        const monthDocCounts = computed(() => {
            const result = {};
            const month = calendarMonth.value;
            const year = month.getFullYear();
            const monthIndex = month.getMonth();
            documents.value.forEach(doc => {
                const timestamp = getTimelineTimestamp(doc);
                if (!timestamp) {
                    return;
                }
                const date = new Date(timestamp * 1000);
                if (date.getFullYear() !== year ||
                    date.getMonth() !== monthIndex) {
                    return;
                }
                const key = dateKey(date);
                result[key] =
                    (result[key] || 0) + 1;
            });
            return result;
        });
        const currentMonthDocCount = computed(() => Object.values(monthDocCounts.value).reduce((sum, value) => sum + value, 0));
        const makeCalendarCell = (date, currentMonth) => {
            const key = dateKey(date);
            const todayKey = dateKey(new Date());
            const selectedKey = dateKey(timelineCursor.value);
            const count = monthDocCounts.value[key] || 0;
            return {
                key,
                date,
                day: date.getDate(),
                currentMonth,
                today: key === todayKey,
                selected: key === selectedKey,
                count
            };
        };
        const calendarCells = computed(() => {
            const month = calendarMonth.value;
            const year = month.getFullYear();
            const monthIndex = month.getMonth();
            const first = new Date(year, monthIndex, 1);
            const firstDay = first.getDay() === 0
                ? 6
                : first.getDay() - 1;
            const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
            const previousMonthDays = new Date(year, monthIndex, 0).getDate();
            const cells = [];
            for (let i = firstDay - 1; i >= 0; i--) {
                const date = new Date(year, monthIndex - 1, previousMonthDays - i);
                cells.push(makeCalendarCell(date, false));
            }
            for (let day = 1; day <= daysInMonth; day++) {
                const date = new Date(year, monthIndex, day);
                cells.push(makeCalendarCell(date, true));
            }
            let nextDay = 1;
            while (cells.length < 42) {
                const date = new Date(year, monthIndex + 1, nextDay++);
                cells.push(makeCalendarCell(date, false));
            }
            return cells;
        });
        const timelineRange = computed(() => {
            if (timelineRangeMode.value ===
                'day') {
                const start = startOfDay(timelineCursor.value);
                const end = new Date(start);
                end.setDate(end.getDate() + 1);
                return {
                    start,
                    end
                };
            }
            const start = startOfWeek(timelineCursor.value);
            const end = new Date(start);
            end.setDate(end.getDate() + 7);
            return {
                start,
                end
            };
        });
        const timelineDocs = computed(() => {
            const start = timelineRange.value.start
                .getTime() / 1000;
            const end = timelineRange.value.end
                .getTime() / 1000;
            return documents.value
                .filter(doc => {
                const timestamp = getTimelineTimestamp(doc);
                return (timestamp >= start &&
                    timestamp < end);
            })
                .sort((a, b) => getTimelineTimestamp(b) -
                getTimelineTimestamp(a));
        });
        const timelineDayGroups = computed(() => {
            const groups = {};
            timelineDocs.value.forEach(doc => {
                const date = new Date(getTimelineTimestamp(doc) * 1000);
                const key = dateKey(date);
                if (!groups[key]) {
                    const names = [
                        '周日',
                        '周一',
                        '周二',
                        '周三',
                        '周四',
                        '周五',
                        '周六'
                    ];
                    groups[key] = {
                        key,
                        label: `${date.getMonth() + 1}月` +
                            `${date.getDate()}日 ` +
                            `${names[date.getDay()]}`,
                        docs: []
                    };
                }
                groups[key].docs.push(doc);
            });
            return Object.values(groups)
                .sort((a, b) => b.key.localeCompare(a.key));
        });
        const selectedDateLabel = computed(() => timelineCursor.value
            .toLocaleDateString('zh-CN', {
            year: 'numeric',
            month: 'long',
            day: 'numeric',
            weekday: 'short'
        }));
        const selectedWeekLabel = computed(() => {
            const start = startOfWeek(timelineCursor.value);
            const end = new Date(start);
            end.setDate(end.getDate() + 6);
            return (`${start.toLocaleDateString('zh-CN', {
                month: 'long',
                day: 'numeric'
            })} ～ ${end.toLocaleDateString('zh-CN', {
                month: 'long',
                day: 'numeric'
            })}`);
        });
        const timelineTitle = computed(() => timelineRangeMode.value ===
            'day'
            ? selectedDateLabel.value
            : selectedWeekLabel.value);
        const moveTimelineMonth = offset => {
            const date = new Date(timelineCursor.value);
            const originalDay = date.getDate();
            date.setDate(1);
            date.setMonth(date.getMonth() + offset);
            const maxDay = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
            date.setDate(Math.min(originalDay, maxDay));
            timelineCursor.value =
                date;
        };
        const timelineToday = () => {
            timelineCursor.value =
                new Date();
        };
        const selectCalendarDay = date => {
            timelineCursor.value =
                new Date(date);
            timelineRangeMode.value =
                'day';
        };
        const toggleTimelinePanel = () => {
            if (sidebarMode.value !== 'timeline') {
                sidebarMode.value =
                    'timeline';
                calendarCollapsed.value =
                    false;
                return;
            }
            calendarCollapsed.value =
                !calendarCollapsed.value;
        };
        const fetchDocuments = async () => {
            try {
                const response = await fetch(API_BASE + '/docapi/documents');
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                documents.value =
                    data.docs || [];
            }
            catch (error) {
                console.error('加载文档失败:', error);
                ElementPlus.ElMessage.error('文档列表加载失败');
            }
        };
        const fetchRelated = async (id) => {
            try {
                const response = await fetch(API_BASE + `/docapi/document/${encodeURIComponent(id)}/related`);
                if (response.ok) {
                    relatedDocs.value =
                        await response.json();
                }
                else {
                    relatedDocs.value = [];
                }
            }
            catch (error) {
                console.error('相关文档加载失败:', error);
                relatedDocs.value = [];
            }
        };
        const createSaveSnapshot = () => {
            return {
                id: docIdInput.value,
                content: editorContent.value,
                created_time: activeDocMeta.value.created_time,
                sequence: ++saveSequence
            };
        };
        const executeSave = async (snapshot) => {
            if (!snapshot ||
                !snapshot.id) {
                return false;
            }
            const targetId = snapshot.id;
            const targetSequence = snapshot.sequence;
            if (docIdInput.value === targetId) {
                saving.value = true;
                saveStatus.value = '保存中...';
            }
            try {
                const response = await fetch(API_BASE + '/docapi/document/save', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        id: targetId,
                        content: snapshot.content,
                        created_time: snapshot.created_time
                    })
                });
                if (!response.ok) {
                    const detail = await response.text()
                        .catch(() => '');
                    throw new Error(`HTTP ${response.status} ${detail}`);
                }
                const data = await response.json()
                    .catch(() => ({}));
                if (data.meta) {
                    if (isCreating.value && !activeDocId.value &&
                        docIdInput.value === targetId) {
                        activeDocId.value = targetId;
                        isCreating.value = false;
                    }
                    syncDocumentMetaToList(data.meta);
                    if (activeDocId.value ===
                        targetId) {
                        activeDocMeta.value = {
                            ...activeDocMeta.value,
                            ...data.meta
                        };
                    }
                }
                if (docIdInput.value ===
                    targetId &&
                    targetSequence ===
                        saveSequence) {
                    dirty.value = false;
                    pendingSnapshot.value =
                        null;
                    saveStatus.value =
                        `已保存 ${new Date().toLocaleTimeString('zh-CN', {
                            hour12: false
                        })}`;
                }
                if (autoAiTags.value)
                    scheduleAutoAiTags();
                return true;
            }
            catch (error) {
                console.error('保存失败:', error);
                if (docIdInput.value ===
                    targetId) {
                    saveStatus.value =
                        '保存失败';
                }
                return false;
            }
            finally {
                if (docIdInput.value ===
                    targetId) {
                    saving.value =
                        false;
                }
            }
        };
        const queueSave = snapshot => {
            saveQueue =
                saveQueue
                    .catch(() => { })
                    .then(() => executeSave(snapshot));
            return saveQueue;
        };
        const flushCurrentDocument = async () => {
            clearTimeout(saveTimer);
            if (!dirty.value) {
                try {
                    const ok = await saveQueue;
                    return ok !== false;
                }
                catch (error) {
                    console.error(error);
                    return false;
                }
            }
            const snapshot = pendingSnapshot.value ||
                createSaveSnapshot();
            pendingSnapshot.value =
                null;
            return await queueSave(snapshot);
        };
        const handleInput = () => {
            dirty.value = true;
            saveStatus.value =
                '有未保存修改';
            clearTimeout(saveTimer);
            const snapshot = createSaveSnapshot();
            pendingSnapshot.value =
                snapshot;
            saveTimer =
                setTimeout(() => {
                    const pending = pendingSnapshot.value;
                    pendingSnapshot.value =
                        null;
                    if (pending) {
                        queueSave(pending);
                    }
                }, 800);
        };
        const manualSave = async () => {
            clearTimeout(saveTimer);
            const snapshot = pendingSnapshot.value ||
                createSaveSnapshot();
            pendingSnapshot.value =
                null;
            await queueSave(snapshot);
        };
        const initOrUpdateVditor = content => {
            if (vditorInstance &&
                isVditorReady) {
                vditorInstance
                    .setValue(content);
                return;
            }
            if (vditorInstance) {
                return;
            }
            vditorInstance =
                new Vditor('vditor-container', {
                    cdn: API_BASE + '/static/cdn/vditor',
                    mode: 'ir',
                    height: '100%',
                    cache: {
                        enable: false
                    },
                    outline: {
                        enable: false,
                        position: 'left'
                    },
                    preview: {
                        hljs: false,
                        math: false,
                        mermaid: false
                    },
                    toolbar: [
                        'headings',
                        'bold',
                        'italic',
                        'strike',
                        'link',
                        '|',
                        'list',
                        'ordered-list',
                        'check',
                        '|',
                        'quote',
                        'line',
                        'code',
                        'inline-code',
                        '|',
                        'table',
                        'undo',
                        'redo',
                        '|',
                        'outline',
                        'fullscreen'
                    ],
                    value: content,
                    input: value => {
                        editorContent.value =
                            value;
                        handleInput();
                        if (activeDocId.value) {
                            const title = parseTitleFromContent(value);
                            if (title) {
                                syncCurrentTitleToList(title);
                                activeDocMeta.value = {
                                    ...activeDocMeta.value,
                                    title
                                };
                            }
                        }
                    },
                    after: () => {
                        isVditorReady =
                            true;
                        vditorInstance
                            .setValue(editorContent.value);
                    }
                });
        };
        const createNew = async () => {
            if (activeDocId.value ||
                dirty.value) {
                const success = await flushCurrentDocument();
                if (!success) {
                    ElementPlus.ElMessage.error('当前文章保存失败，无法创建新文章');
                    return;
                }
            }
            const timestamp = Date.now();
            activeDocId.value =
                '';
            isCreating.value =
                true;
            docIdInput.value =
                `note_${timestamp}`;
            activeDocMeta.value = {
                id: `note_${timestamp}`,
                title: `随记_${timestamp}`,
                created_time: Math.floor(timestamp / 1000),
                update_time: Math.floor(timestamp / 1000),
                is_pinned: 0,
                status: 'active'
            };
            relatedDocs.value = [];
            generatedTags.value = [];
            dirty.value = false;
            pendingSnapshot.value =
                null;
            editorContent.value =
                `---
title: 随记_${timestamp}
tags:
  - 笔记
---

`;
            saveStatus.value =
                '新建待存';
            sidebarMode.value =
                'docs';
            nextTick(() => {
                initOrUpdateVditor(editorContent.value);
            });
            if (isMobile.value) {
                mobileView.value =
                    'editor';
            }
        };
        const loadDocument = async (id) => {
            if ((activeDocId.value && activeDocId.value !== id) ||
                isCreating.value) {
                const success = await flushCurrentDocument();
                if (!success) {
                    ElementPlus.ElMessage.error('当前文章保存失败，已取消切换');
                    return;
                }
            }
            try {
                const response = await fetch(API_BASE + `/docapi/document/${encodeURIComponent(id)}`);
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                activeDocId.value =
                    id;
                docIdInput.value =
                    id;
                editorContent.value =
                    data.content || '';
                activeDocMeta.value =
                    data.meta || {
                        id,
                        title: '',
                        created_time: null,
                        update_time: null,
                        is_pinned: 0
                    };
                isCreating.value =
                    false;
                dirty.value = false;
                pendingSnapshot.value =
                    null;
                saveStatus.value =
                    '已加载';
                relatedDocs.value =
                    [];
                generatedTags.value =
                    [];
                if (data.meta) {
                    syncDocumentMetaToList(data.meta);
                }
                nextTick(() => {
                    initOrUpdateVditor(editorContent.value);
                });
                fetchRelated(id);
                if (isMobile.value) {
                    mobileView.value =
                        'editor';
                }
            }
            catch (error) {
                console.error('加载文档失败:', error);
                ElementPlus.ElMessage.error('文档加载失败');
            }
        };
        const loadDocumentFromTimeline = async (id) => {
            await loadDocument(id);
        };
        const flushAndBack = async () => {
            const success = await flushCurrentDocument();
            if (!success) {
                ElementPlus.ElMessage.error('保存失败，暂不返回');
                return;
            }
            mobileView.value =
                'list';
        };
        const resetEditorState = () => {
            activeDocId.value = '';
            docIdInput.value = '';
            editorContent.value = '';
            isCreating.value = false;
            dirty.value = false;
            pendingSnapshot.value = null;
            relatedDocs.value = [];
            generatedTags.value = [];
            saveStatus.value = '';
            activeDocMeta.value = {
                id: '',
                title: '',
                created_time: null,
                update_time: null,
                is_pinned: 0,
                status: 'active'
            };
            if (vditorInstance && isVditorReady) {
                vditorInstance.setValue('');
            }
            if (isMobile.value) {
                mobileView.value = 'list';
            }
        };
        const deleteDocument = async () => {
            if (isCreating.value && !activeDocId.value) {
                let confirmed = false;
                try {
                    await ElementPlus.ElMessageBox.confirm('放弃这篇尚未保存的笔记？', '提示', {
                        type: 'warning',
                        confirmButtonText: '放弃',
                        cancelButtonText: '继续编辑'
                    });
                    confirmed = true;
                }
                catch (e) {
                    confirmed = false;
                }
                if (!confirmed) {
                    return;
                }
                clearTimeout(saveTimer);
                pendingSnapshot.value = null;
                resetEditorState();
                ElementPlus.ElMessage.success('已放弃草稿');
                return;
            }
            const id = activeDocId.value;
            if (!id) {
                return;
            }
            let confirmed = false;
            try {
                await ElementPlus.ElMessageBox.confirm(`确定删除「${getDocTitle(activeDocMeta.value || { id })}」吗？\n` +
                    '文件会移入服务器回收站，可手动找回。', '删除确认', {
                    type: 'warning',
                    confirmButtonText: '删除',
                    cancelButtonText: '取消',
                    confirmButtonClass: 'el-button--danger'
                });
                confirmed = true;
            }
            catch (e) {
                confirmed = false;
            }
            if (!confirmed) {
                return;
            }
            clearTimeout(saveTimer);
            pendingSnapshot.value = null;
            dirty.value = false;
            try {
                await saveQueue;
            }
            catch (e) {
                console.error(e);
            }
            deleting.value = true;
            try {
                const response = await fetch(API_BASE + '/docapi/document/delete', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ id })
                });
                const data = await response.json()
                    .catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.error ||
                        `HTTP ${response.status}`);
                }
                documents.value =
                    documents.value.filter(doc => doc.id !== id);
                searchResults.value =
                    searchResults.value.filter(doc => doc.id !== id);
                resetEditorState();
                ElementPlus.ElMessage.success('已删除');
            }
            catch (error) {
                console.error('删除失败:', error);
                ElementPlus.ElMessage.error(error.message || '删除失败');
            }
            finally {
                deleting.value = false;
            }
        };
        const commitRename = async () => {
            if (isCreating.value) {
                return;
            }
            if (renaming.value) {
                return;
            }
            const oldId = activeDocId.value;
            if (!oldId) {
                return;
            }
            const newId = String(docIdInput.value || '').trim();
            if (!newId) {
                docIdInput.value =
                    oldId;
                return;
            }
            if (newId === oldId) {
                return;
            }
            const saved = await flushCurrentDocument();
            if (!saved) {
                docIdInput.value =
                    oldId;
                ElementPlus.ElMessage.error('当前文章保存失败，无法重命名');
                return;
            }
            renaming.value =
                true;
            try {
                const response = await fetch(API_BASE + '/docapi/document/rename', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        old_id: oldId,
                        new_id: newId
                    })
                });
                const data = await response.json()
                    .catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.error ||
                        `HTTP ${response.status}`);
                }
                const actualNewId = (data.meta && data.meta.id) ||
                    newId;
                activeDocId.value =
                    actualNewId;
                docIdInput.value =
                    actualNewId;
                if (data.meta) {
                    activeDocMeta.value = {
                        ...activeDocMeta.value,
                        ...data.meta
                    };
                    documents.value =
                        documents.value.filter(doc => doc.id !==
                            oldId);
                    syncDocumentMetaToList(data.meta);
                }
                saveStatus.value =
                    '重命名成功';
                await fetchDocuments();
            }
            catch (error) {
                console.error('重命名失败:', error);
                docIdInput.value =
                    oldId;
                ElementPlus.ElMessage.error(error.message ||
                    '文件名修改失败');
            }
            finally {
                renaming.value =
                    false;
            }
        };
        const toggleAction = async (action) => {
            if (!activeDocId.value) {
                return;
            }
            const saved = await flushCurrentDocument();
            if (!saved) {
                ElementPlus.ElMessage.error('当前文章保存失败，操作已取消');
                return;
            }
            const currentId = activeDocId.value;
            try {
                const response = await fetch(API_BASE + '/docapi/document/action', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        id: currentId,
                        action
                    })
                });
                const data = await response.json()
                    .catch(() => ({}));
                if (!response.ok) {
                    throw new Error(data.error ||
                        `HTTP ${response.status}`);
                }
                if (data.meta) {
                    activeDocMeta.value = {
                        ...activeDocMeta.value,
                        ...data.meta
                    };
                    syncDocumentMetaToList(data.meta);
                }
                await fetchDocuments();
                const reloadResponse = await fetch(API_BASE + `/docapi/document/${encodeURIComponent(currentId)}`);
                if (reloadResponse.ok) {
                    const reloadData = await reloadResponse.json();
                    editorContent.value =
                        reloadData.content || '';
                    activeDocMeta.value =
                        reloadData.meta || activeDocMeta.value;
                    dirty.value =
                        false;
                    pendingSnapshot.value =
                        null;
                    nextTick(() => {
                        initOrUpdateVditor(editorContent.value);
                    });
                }
            }
            catch (error) {
                console.error('操作失败:', error);
                ElementPlus.ElMessage.error(error.message ||
                    '操作失败');
            }
        };
        const generateTags = async () => {
            if (!editorContent.value.trim()) {
                return;
            }
            aiLoading.value =
                true;
            try {
                const response = await fetch(API_BASE + '/docapi/document/ai/tags', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({
                        content: editorContent.value
                    })
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                if (data.tags?.includes('API_KEY_MISSING')) {
                    throw new Error('未配置 OP_API_KEY，无法生成 AI 标签');
                }
                if (data.tags?.includes('AI_ERROR')) {
                    throw new Error('AI 服务暂时不可用');
                }
                if (data.tags &&
                    Array.isArray(data.tags) &&
                    data.tags.length > 0) {
                    generatedTags.value =
                        data.tags;
                }
                else {
                    ElementPlus.ElMessage.warning('未能提取有效标签');
                }
            }
            catch (error) {
                console.error(error);
                ElementPlus.ElMessage.error(error.message || 'AI 解析失败');
            }
            finally {
                aiLoading.value =
                    false;
            }
        };
        const applyGeneratedTags = () => {
            if (!generatedTags.value.length || (!activeDocId.value && !isCreating.value))
                return;
            const newLine = `tags: [${generatedTags.value.map(tag => JSON.stringify(tag)).join(', ')}]`;
            const original = editorContent.value || '';
            const front = original.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/);
            let result;
            if (front) {
                const lines = front[1].split(/\r?\n/);
                const position = lines.findIndex(line => /^tags\s*:/.test(line));
                if (position >= 0) {
                    let last = position + 1;
                    while (last < lines.length && /^[ \t]+\S/.test(lines[last]))
                        last++;
                    lines.splice(position, last - position, newLine);
                }
                else {
                    lines.push(newLine);
                }
                result = `---\n${lines.join('\n')}\n---\n${original.slice(front[0].length)}`;
            }
            else {
                result = `---\n${newLine}\n---\n${original}`;
            }
            editorContent.value = result;
            activeDocMeta.value = { ...activeDocMeta.value,
                tags: [...generatedTags.value], tags_cache: generatedTags.value.join(',') };
            if (vditorInstance && isVditorReady)
                vditorInstance.setValue(result);
            handleInput();
            ElementPlus.ElMessage.success('标签已写入笔记，正在自动保存');
        };
        const rebuildIndex = async () => {
            syncing.value =
                true;
            try {
                const saved = await flushCurrentDocument();
                if (!saved) {
                    ElementPlus.ElMessage.error('当前文章保存失败，无法同步');
                    return;
                }
                const response = await fetch(API_BASE + '/docapi/init', {
                    method: 'POST'
                });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                await fetchDocuments();
                ElementPlus.ElMessage.success('索引同步完成');
            }
            catch (error) {
                console.error(error);
                ElementPlus.ElMessage.error('索引同步失败');
            }
            finally {
                syncing.value =
                    false;
            }
        };
        const handleBeforeUnload = event => {
            if (dirty.value ||
                saving.value) {
                event.preventDefault();
                event.returnValue = '';
            }
        };
        window.addEventListener('beforeunload', handleBeforeUnload);
        const handleGlobalKeyDown = event => {
            const ctrl = event.ctrlKey || event.metaKey;
            if (ctrl && event.key.toLowerCase() === 's') {
                event.preventDefault();
                if (activeDocId.value || isCreating.value)
                    manualSave();
            }
            else if (ctrl && event.key.toLowerCase() === 'k') {
                event.preventDefault();
                if (isFocusMode.value)
                    isFocusMode.value = false;
                if (isMobile.value)
                    mobileView.value = 'list';
                nextTick(() => document.querySelector('.header-search input')?.focus());
            }
            else if (ctrl && event.key.toLowerCase() === 'n') {
                event.preventDefault();
                createNew();
            }
            else if (event.key === 'Escape' && isFocusMode.value) {
                isFocusMode.value = false;
            }
        };
        onMounted(async () => {
            updateIsMobile();
            window.addEventListener('keydown', handleGlobalKeyDown);
            await fetchDocuments();
        });
        onUnmounted(() => {
            window.removeEventListener('keydown', handleGlobalKeyDown);
            window.removeEventListener('resize', updateIsMobile);
            window.removeEventListener('beforeunload', handleBeforeUnload);
            clearTimeout(saveTimer);
            clearTimeout(searchTimer);
            clearTimeout(autoAiTimer);
            vditorInstance?.destroy?.();
        });
        return {
            documents,
            searchResults,
            searchQuery,
            activeDocId,
            docIdInput,
            editorContent,
            isCreating,
            activeDocMeta,
            relatedDocs,
            saving,
            syncing,
            isSearching,
            renaming,
            clipboardTitleLoading,
            deleting,
            saveStatus,
            dirty,
            aiLoading,
            generatedTags,
            isFocusMode,
            autoAiTags,
            isMobile,
            mobileView,
            sidebarMode,
            pinnedDocs,
            inboxDocs,
            archiveDocs,
            activeDocStatus,
            createdTimeAgo,
            updatedTimeAgo,
            fullCreatedTime,
            fullUpdateTime,
            charCount,
            totalChars,
            lineCount,
            paragraphCount,
            readTime,
            timelineCursor,
            timelineTimeField,
            timelineRangeMode,
            calendarCollapsed,
            weekNames,
            calendarTitle,
            calendarCells,
            currentMonthDocCount,
            timelineTitle,
            selectedDateLabel,
            selectedWeekLabel,
            timelineDocs,
            timelineDayGroups,
            getDocTitle,
            getDocPreview,
            highlightText,
            createNew,
            loadDocument,
            loadDocumentFromTimeline,
            manualSave,
            handleInput,
            setTitleFromClipboard,
            forceSearch,
            rebuildIndex,
            toggleAction,
            generateTags,
            applyGeneratedTags,
            commitRename,
            deleteDocument,
            resetEditorState,
            moveTimelineMonth,
            timelineToday,
            selectCalendarDay,
            toggleTimelinePanel,
            flushAndBack,
            formatTimelineTime
        };
    }
});
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
    app.component(key, component);
}
app.use(ElementPlus);
app.mount('#app');
</script>


<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">文档笔记 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
(function(){
  "use strict";
  var BASE = window.API_BASE || location.pathname.replace(/\/[^/]*$/, "");
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "文档笔记 · 首次设置" : "文档笔记 · 登录";
    document.getElementById("authBtn").textContent = setup ? "创建账户并进入" : "登录";
    document.getElementById("authForm").dataset.mode = setup ? "setup" : "login";
  }
  fetch(BASE + "/api/session", {cache:"no-store"})
    .then(function(r){return r.json();})
    .then(function(s){ if(!s.authenticated) showGate(!!s.setup_required); })
    .catch(function(){ showGate(false); });
  document.getElementById("authForm").addEventListener("submit", function(e){
    e.preventDefault();
    var mode = e.target.dataset.mode || "login";
    fetch(BASE + "/api/" + mode, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        username: document.getElementById("authUser").value.trim(),
        password: document.getElementById("authPass").value
      })
    }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
      .then(function(res){
        if(res.ok){ location.reload(); }
        else{ document.getElementById("authErr").textContent = (res.d && res.d.message) || "登录失败，请重试"; }
      })
      .catch(function(){ document.getElementById("authErr").textContent = "网络错误，请重试"; });
  });
})();
</script>
</body>
</html>''',
    'view': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport"
          content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>文档时间线 · 个人知识笔记</title>

    <script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>

    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <link rel="stylesheet" href="static/cdn/element-plus2.14.3.css">
    <script src="static/cdn/element-plus2.14.3.js"></script>

    <style>
        * { box-sizing: border-box; }
        html, body, #app { width: 100%; height: 100%; margin: 0; }
        body {
            overflow: hidden; background: #f8fafc;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
            -webkit-font-smoothing: antialiased; color: #1e293b;
        }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }

        .layout { display: flex; height: 100%; width: 100%; }
        .sidebar {
            width: 300px; min-width: 300px; background: #fff;
            border-right: 1px solid #e5e7eb;
            display: flex; flex-direction: column; overflow: hidden;
        }
        .main {
            flex: 1; min-width: 0; display: flex; flex-direction: column;
            overflow: hidden; background: #f8fafc;
        }

        .topbar {
            height: 56px; flex-shrink: 0; background: #fff;
            border-bottom: 1px solid #e5e7eb;
            display: flex; align-items: center; gap: 10px;
            padding: 0 14px; z-index: 20;
        }
        .brand {
            font-weight: 700; font-size: 15px; color: #334155;
            display: flex; align-items: center; gap: 6px; white-space: nowrap;
        }
        .search-wrap { flex: 1; max-width: 420px; min-width: 120px; }
        .topbar-right { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
        .stat-mini { font-size: 11px; color: #94a3b8; white-space: nowrap; }

        .view-switch {
            display: flex; align-items: center; gap: 0;
            border: 1px solid #e2e8f0; border-radius: 6px;
            overflow: hidden; flex-shrink: 0;
        }
        .view-switch-btn {
            padding: 4px 10px; font-size: 12px; color: #64748b;
            background: #fff; cursor: pointer; user-select: none;
            transition: all 0.12s; border: none;
        }
        .view-switch-btn:hover { background: #f8fafc; color: #334155; }
        .view-switch-btn.active {
            background: #eff6ff; color: #1d4ed8; font-weight: 600;
        }
        .view-switch-btn + .view-switch-btn { border-left: 1px solid #e2e8f0; }

        .sidebar-head {
            padding: 12px 14px 10px; flex-shrink: 0;
            border-bottom: 1px solid #f1f5f9;
        }
        .sidebar-head-row {
            display: flex; align-items: center; justify-content: space-between;
            gap: 8px;
        }
        .sidebar-title {
            font-size: 12px; font-weight: 700; color: #64748b;
            letter-spacing: 0.4px;
        }
        .stat-btn {
            font-size: 11px; color: #2563eb; cursor: pointer;
            padding: 2px 8px; border-radius: 4px; background: #eff6ff;
            border: 1px solid #dbeafe; user-select: none;
            white-space: nowrap;
        }
        .stat-btn:hover { background: #dbeafe; }

        .sidebar-summary {
            margin-top: 8px;
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 6px;
        }
        .sum-item {
            background: #f8fafc; border-radius: 6px;
            padding: 6px 8px; text-align: center;
        }
        .sum-value {
            font-size: 14px; font-weight: 700; color: #1e293b;
            line-height: 1.2;
        }
        .sum-label { font-size: 10px; color: #94a3b8; margin-top: 2px; }

        .sidebar-body { flex: 1; overflow-y: auto; padding: 6px 8px 16px; }

        .cat-item {
            display: flex; align-items: center; gap: 8px;
            padding: 7px 10px; border-radius: 6px;
            cursor: pointer; user-select: none;
            font-size: 13px; color: #334155;
            transition: background 0.12s;
        }
        .cat-item:hover { background: #f1f5f9; }
        .cat-item.active { background: #eff6ff; color: #1d4ed8; font-weight: 600; }
        .cat-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
        .cat-name {
            flex: 1; min-width: 0;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .cat-name small {
            color: #94a3b8; font-weight: 400; margin-left: 4px; font-size: 10px;
        }
        .cat-count {
            font-size: 10px; color: #94a3b8; background: #f1f5f9;
            padding: 2px 7px; border-radius: 8px; font-weight: 600;
            display: flex; align-items: center; gap: 4px;
            flex-shrink: 0;
        }
        .cat-count .avg {
            font-weight: 400; color: #94a3b8;
            border-left: 1px solid #cbd5e1; padding-left: 4px;
        }
        .cat-item.active .cat-count { background: #dbeafe; color: #1d4ed8; }
        .cat-item.active .cat-count .avg { color: #60a5fa; border-color: #93c5fd; }

        .subcat-list {
            margin-left: 22px; border-left: 1px dashed #e2e8f0;
            padding-left: 4px; margin-bottom: 4px;
        }
        .subcat-item {
            display: flex; align-items: center; gap: 6px;
            padding: 4px 8px; border-radius: 5px;
            cursor: pointer; font-size: 12px; color: #64748b;
        }
        .subcat-item:hover { background: #f8fafc; color: #334155; }
        .subcat-item.active {
            background: #f0f9ff; color: #0369a1; font-weight: 600;
        }
        .subcat-stats {
            margin-left: auto; font-size: 10px; color: #94a3b8;
            display: flex; gap: 6px;
        }

        .cat-arrow {
            width: 14px; height: 14px; flex-shrink: 0;
            display: inline-flex; align-items: center; justify-content: center;
            font-size: 9px; color: #94a3b8; transition: transform 0.15s;
        }
        .cat-arrow.open { transform: rotate(90deg); }

        .doc-list-wrap { flex: 1; overflow-y: auto; padding: 14px 16px 40px; }

        .doc-group { margin-bottom: 22px; }
        .doc-group-head {
            display: flex; align-items: center; gap: 8px;
            margin: 0 4px 10px; cursor: pointer; user-select: none;
        }
        .doc-group-head:hover .doc-group-name { color: #1d4ed8; }
        .doc-group-name {
            font-size: 14px; font-weight: 700; color: #334155;
            display: flex; align-items: center; gap: 6px;
        }
        .doc-group-count { font-size: 11px; color: #94a3b8; font-weight: 500; }
        .doc-group-count .avg {
            margin-left: 4px; padding-left: 6px;
            border-left: 1px solid #e2e8f0; color: #64748b;
        }
        .doc-group-line { flex: 1; height: 1px; background: #e2e8f0; }

        .doc-card {
            background: #fff; border: 1px solid #e5e7eb; border-radius: 10px;
            padding: 12px 14px; margin-bottom: 8px; cursor: pointer;
            transition: all 0.14s;
            display: flex; flex-direction: column; gap: 6px;
        }
        .doc-card:hover {
            border-color: #93c5fd;
            box-shadow: 0 2px 8px rgba(59, 130, 246, 0.08);
            transform: translateY(-1px);
        }
        .doc-card-title {
            font-size: 14px; font-weight: 600; line-height: 1.5;
            color: #1e293b; word-break: break-word;
        }
        .doc-card-preview {
            font-size: 12px; color: #64748b; line-height: 1.55;
            display: -webkit-box; -webkit-box-orient: vertical;
            -webkit-line-clamp: 2; overflow: hidden;
        }
        .doc-card-meta {
            display: flex; align-items: center; gap: 10px;
            font-size: 10px; color: #94a3b8;
            margin-top: 2px; flex-wrap: wrap;
        }
        .doc-card-meta .meta-tag {
            background: #f1f5f9; padding: 1px 6px;
            border-radius: 4px; font-weight: 500;
        }

        .timeline-wrap {
            flex: 1; overflow-y: auto;
            padding: 14px 16px 60px;
        }
        .timeline-inner { max-width: 900px; margin: 0 auto; }

        .timeline-bar {
            display: flex; align-items: center; gap: 10px;
            margin-bottom: 16px; padding: 8px 12px;
            background: #fff; border: 1px solid #e5e7eb;
            border-radius: 8px;
            position: sticky; top: 0; z-index: 5;
        }
        .timeline-bar-label {
            font-size: 12px; color: #64748b; white-space: nowrap;
        }
        .timeline-bar-count {
            margin-left: auto; font-size: 11px; color: #94a3b8;
            white-space: nowrap;
        }

        .timeline-group { margin-bottom: 26px; }

        .timeline-date {
            display: flex; align-items: center; gap: 8px;
            margin: 0 0 10px 2px;
            position: sticky; top: 52px; z-index: 3;
            background: linear-gradient(to bottom, #f8fafc 60%, transparent);
            padding: 6px 0;
        }
        .timeline-date-dot {
            width: 10px; height: 10px; border-radius: 50%;
            background: #3b82f6;
            box-shadow: 0 0 0 3px #dbeafe;
        }
        .timeline-date-text {
            font-size: 14px; font-weight: 700; color: #334155;
        }
        .timeline-date-count {
            font-size: 11px; color: #94a3b8;
            background: #f1f5f9; padding: 1px 8px; border-radius: 8px;
        }
        .timeline-date-line {
            flex: 1; height: 1px; background: #e2e8f0;
        }

        .timeline-items {
            margin-left: 4px;
            padding-left: 20px;
            border-left: 2px solid #e2e8f0;
        }

        .timeline-item {
            display: flex; align-items: flex-start; gap: 10px;
            padding: 10px 12px; margin-bottom: 6px;
            background: #fff; border: 1px solid #e5e7eb;
            border-radius: 8px; cursor: pointer;
            transition: all 0.14s;
            position: relative;
        }
        .timeline-item::before {
            content: '';
            position: absolute; left: -27px; top: 18px;
            width: 8px; height: 8px; border-radius: 50%;
            background: #cbd5e1; border: 2px solid #fff;
        }
        .timeline-item:hover {
            border-color: #93c5fd;
            box-shadow: 0 2px 8px rgba(59, 130, 246, 0.08);
            transform: translateX(2px);
        }
        .timeline-item:hover::before {
            background: #3b82f6;
        }

        .timeline-item-time {
            flex-shrink: 0; font-size: 11px; color: #94a3b8;
            font-variant-numeric: tabular-nums;
            width: 40px; padding-top: 3px;
        }
        .timeline-item-body {
            flex: 1; min-width: 0;
            display: flex; flex-direction: column; gap: 3px;
        }
        .timeline-item-title {
            font-size: 13.5px; font-weight: 600; color: #1e293b;
            line-height: 1.45; word-break: break-word;
        }
        .timeline-item-preview {
            font-size: 12px; color: #64748b; line-height: 1.5;
            display: -webkit-box; -webkit-box-orient: vertical;
            -webkit-line-clamp: 1; overflow: hidden;
        }
        .timeline-item-meta {
            display: flex; align-items: center; gap: 8px;
            font-size: 10px; color: #94a3b8; margin-top: 2px;
            flex-wrap: wrap;
        }
        .timeline-item-words {
            flex-shrink: 0; font-size: 10px; color: #94a3b8;
            padding-top: 3px;
            font-variant-numeric: tabular-nums;
        }

        /* =========================================================
           分类徽章 —— 放大 + 可点击
           ========================================================= */
        .cat-badge {
            display: inline-flex; align-items: center; gap: 4px;
            font-size: 12px;
            padding: 3px 9px;
            border-radius: 5px;
            font-weight: 600;
            letter-spacing: 0.3px;
            flex-shrink: 0;
            cursor: pointer;
            user-select: none;
            transition: transform 0.12s ease, box-shadow 0.12s ease,
                        filter 0.12s ease;
            transform-origin: center;
        }
        .cat-badge:hover {
            transform: scale(1.08);
            box-shadow: 0 2px 8px rgba(15, 23, 42, 0.12);
            filter: brightness(0.96);
        }
        .cat-badge:active {
            transform: scale(0.98);
        }

        .empty-hint {
            text-align: center; padding: 60px 20px;
            color: #94a3b8; font-size: 13px;
        }

        .reader {
            flex: 1; overflow-y: auto;
            display: flex; justify-content: center;
            padding: 26px 16px 60px;
        }
        .reader-inner { width: 100%; max-width: 780px; }
        .reader-head {
            margin-bottom: 22px; padding-bottom: 16px;
            border-bottom: 1px solid #e2e8f0;
        }
        .reader-title {
            font-size: 24px; font-weight: 700; line-height: 1.4;
            color: #0f172a; margin: 10px 0 12px; word-break: break-word;
        }
        .reader-meta {
            display: flex; flex-wrap: wrap; align-items: center;
            gap: 12px; font-size: 12px; color: #64748b;
        }
        .reader-meta-item { display: inline-flex; align-items: center; gap: 4px; }

        .reader-body {
            font-size: 15.5px; line-height: 1.85; color: #1e293b;
            word-break: break-word;
        }
        .reader-body h1 {
            font-size: 22px; margin: 26px 0 14px;
            padding-bottom: 8px; border-bottom: 1px solid #e2e8f0;
        }
        .reader-body h2 { font-size: 19px; margin: 22px 0 12px; }
        .reader-body h3 { font-size: 17px; margin: 18px 0 10px; }
        .reader-body h4 { font-size: 16px; margin: 16px 0 8px; }
        .reader-body h5,
        .reader-body h6 { font-size: 15px; margin: 14px 0 8px; }
        .reader-body p { margin: 10px 0; }
        .reader-body ul,
        .reader-body ol { margin: 10px 0; padding-left: 26px; }
        .reader-body li { margin: 4px 0; }
        .reader-body blockquote {
            border-left: 3px solid #93c5fd;
            padding: 6px 14px; margin: 12px 0;
            background: #f8fafc; color: #475569;
        }
        .reader-body code {
            background: #f1f5f9; padding: 1px 5px;
            border-radius: 3px; font-size: 0.92em; color: #be185d;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }
        .reader-body pre {
            background: #1e293b; border-radius: 8px;
            padding: 14px; margin: 14px 0; overflow-x: auto;
        }
        .reader-body pre code {
            background: transparent; color: #e2e8f0;
            padding: 0; font-size: 13px; line-height: 1.6;
        }
        .reader-body table {
            border-collapse: collapse; margin: 14px 0;
            font-size: 13px; width: 100%;
            display: block; overflow-x: auto; max-width: 100%;
        }
        .reader-body th,
        .reader-body td { border: 1px solid #e2e8f0; padding: 7px 10px; }
        .reader-body th { background: #f8fafc; font-weight: 600; }
        .reader-body img { max-width: 100%; border-radius: 6px; margin: 8px 0; }
        .reader-body a { color: #2563eb; text-decoration: none; }
        .reader-body a:hover { text-decoration: underline; }
        .reader-body hr { border: none; border-top: 1px solid #e2e8f0; margin: 20px 0; }

        .mobile-only { display: none; }

        @media (max-width: 768px) {
            .mobile-only { display: inline-flex; }
            .sidebar {
                position: fixed; top: 0; left: 0; bottom: 0; z-index: 100;
                transform: translateX(-100%);
                box-shadow: 2px 0 16px rgba(15, 23, 42, 0.12);
                width: 84vw; min-width: 84vw; max-width: 340px;
            }
            .sidebar.open { transform: translateX(0); }
            .sidebar-mask {
                position: fixed; inset: 0;
                background: rgba(15, 23, 42, 0.42);
                z-index: 99;
            }
            .topbar { height: 52px; padding: 0 10px; }
            .brand span.brand-text { display: none; }
            .stat-mini { display: none; }
            .search-wrap { max-width: none; }
            .doc-list-wrap { padding: 10px 10px 30px; }
            .doc-card { padding: 11px 12px; }
            .doc-card-title { font-size: 13.5px; }
            .reader { padding: 16px 14px 40px; }
            .reader-title { font-size: 20px; margin: 8px 0 10px; }
            .reader-body { font-size: 15px; }

            .view-switch-btn { padding: 3px 8px; font-size: 11px; }

            .timeline-wrap { padding: 10px 10px 40px; }
            .timeline-bar { padding: 6px 10px; gap: 6px; }
            .timeline-bar-label { font-size: 11px; }
            .timeline-date { top: 48px; }
            .timeline-date-text { font-size: 13px; }
            .timeline-items { padding-left: 14px; }
            .timeline-item {
                padding: 9px 10px; gap: 8px;
            }
            .timeline-item::before { left: -21px; }
            .timeline-item-time { width: 34px; font-size: 10px; }
            .timeline-item-title { font-size: 13px; }

            /* 移动端稍减，但仍比旧版大 */
            .cat-badge { font-size: 11.5px; padding: 3px 8px; }
        }

        .reader-bar {
            height: 52px; flex-shrink: 0; background: #fff;
            border-bottom: 1px solid #e5e7eb;
            display: flex; align-items: center; gap: 10px; padding: 0 14px;
        }
        .reader-bar-title {
            flex: 1; font-size: 13px; font-weight: 600; color: #475569;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }

        .hl { background: #fef08a; color: #92400e; border-radius: 2px; padding: 0 1px; }

        .stats-panel { padding: 4px 2px; }
        .stats-grid {
            display: grid; grid-template-columns: repeat(4, 1fr);
            gap: 8px; margin-bottom: 14px;
        }
        .stats-cell {
            background: #f8fafc; border: 1px solid #f1f5f9;
            border-radius: 8px; padding: 10px 8px; text-align: center;
        }
        .stats-cell .v {
            font-size: 18px; font-weight: 700; color: #1e293b;
            line-height: 1.2;
        }
        .stats-cell .l { font-size: 11px; color: #94a3b8; margin-top: 4px; }
        .stats-section-title {
            font-size: 12px; font-weight: 700; color: #64748b;
            margin: 14px 0 8px;
        }
        .stats-table {
            width: 100%; border-collapse: collapse; font-size: 12px;
        }
        .stats-table th {
            text-align: left; color: #94a3b8; font-weight: 600;
            padding: 6px 8px; border-bottom: 1px solid #e5e7eb;
            font-size: 11px;
        }
        .stats-table td {
            padding: 6px 8px; border-bottom: 1px solid #f1f5f9;
            color: #334155;
        }
        .stats-table tr:hover td { background: #f8fafc; }
        .stats-table .num { text-align: right; font-variant-numeric: tabular-nums; }
        .stats-top-list { font-size: 12px; color: #475569; }
        .stats-top-item {
            display: flex; justify-content: space-between; gap: 10px;
            padding: 5px 8px; border-radius: 5px; cursor: pointer;
            align-items: center;
        }
        .stats-top-item:hover { background: #f1f5f9; }
        .stats-top-item .t {
            flex: 1; overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap;
        }
        .stats-top-item .w {
            color: #94a3b8; flex-shrink: 0; font-variant-numeric: tabular-nums;
        }
        @media (max-width: 768px) {
            .stats-grid { grid-template-columns: repeat(3, 1fr); }
        }

        /* ==================== 2026 UI / responsive refresh ==================== */
        :root {
            color-scheme: light;
            --page: #f3f6fb;
            --surface: #ffffff;
            --line: #e5eaf2;
            --ink: #17243a;
            --muted: #738197;
            --primary: #4f46e5;
            --primary-soft: #eef2ff;
            --radius: 14px;
            --shadow: 0 8px 28px rgba(25, 40, 70, .045);
        }
        html, body, #app { min-height: 100%; }
        #app { height: 100dvh; }
        body, .main { background: var(--page); color: var(--ink); }
        button, input { font: inherit; }
        button { -webkit-tap-highlight-color: transparent; }
        button:focus-visible, input:focus-visible, .cat-item:focus-visible {
            outline: 3px solid rgba(79,70,229,.28);
            outline-offset: 2px;
        }
        .sidebar { width: 286px; min-width: 286px; border-color: var(--line); }
        .sidebar-head { padding: 18px 16px 14px; }
        .sidebar-title { color: var(--ink); font-size: 13px; letter-spacing: .2px; }
        .sidebar-summary { gap: 7px; }
        .sum-item { background: #f6f8fc; border-radius: 10px; padding: 10px 5px; }
        .sum-value { font-size: 17px; color: var(--ink); }
        .sum-label { font-size: 11px; }
        .cat-item { border-radius: 9px; padding: 10px 11px; }
        .cat-item.active { background: var(--primary-soft); color: #4338ca; }
        .subcat-item { border-radius: 8px; padding: 8px; }
        .topbar, .reader-bar { min-height: 68px; height: auto; background: rgba(255,255,255,.96); }
        .topbar { padding: 10px 22px; gap: 16px; }
        .brand { font-size: 16px; color: var(--ink); }
        .topbar-right { gap: 10px; }
        .view-switch { border-radius: 10px; background: #f6f8fc; padding: 3px; border-color: #edf1f7; }
        .view-switch-btn { border-radius: 7px; min-height: 31px; padding: 6px 14px; font-size: 12px; }
        .view-switch-btn + .view-switch-btn { border-left: 0; }
        .view-switch-btn.active { background: #fff; box-shadow: 0 2px 8px rgba(10,20,50,.09); color: #4338ca; }
        .doc-list-wrap { padding: 22px 26px 52px; }
        .doc-group { max-width: 1040px; margin-left: auto; margin-right: auto; }
        .doc-card { border-color: var(--line); border-radius: var(--radius); padding: 17px 19px; box-shadow: var(--shadow); }
        .doc-card:hover { border-color: #a5b4fc; box-shadow: 0 7px 22px rgba(79,70,229,.10); }
        .timeline-wrap { padding: 20px 25px 70px; overscroll-behavior: contain; }
        .timeline-inner { max-width: 1040px; }
        .timeline-toolbar {
            background: var(--surface); border: 1px solid var(--line);
            border-radius: var(--radius); box-shadow: var(--shadow);
            padding: 16px 18px; margin-bottom: 15px;
            display: flex; flex-direction: column; gap: 13px;
        }
        .timeline-toolbar-row, .timeline-control, .timeline-presets,
        .timeline-range { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .timeline-toolbar-row { justify-content: space-between; gap: 12px 20px; }
        .timeline-control { gap: 8px; }
        .control-caption { font-size: 12px; color: var(--muted); font-weight: 600; white-space: nowrap; }
        .timeline-presets { gap: 8px; }
        .filter-pill {
            color: #53627b; padding: 8px 12px; min-height: 36px; line-height: 1.2;
            background: #f6f8fc; border: 1px solid transparent;
            border-radius: 9px; cursor: pointer; font-size: 12px; font-weight: 600;
        }
        .filter-pill:hover { background: #e9edfb; }
        .filter-pill.active { background: var(--primary-soft); color: #4338ca; border-color: #c7d2fe; }
        .timeline-range { gap: 10px; }
        .date-field {
            display: inline-flex; align-items: center; gap: 10px; flex: 0 0 auto;
            background: #f8fafd; padding: 5px 10px;
            border: 1px solid var(--line); border-radius: 9px;
            color: var(--muted); font-size: 12px;
        }
        .date-field input {
            background: transparent; border: 0; outline: none; color: var(--ink);
            min-height: 27px; font-size: 13px; min-width: 122px; max-width: 155px;
        }
        .text-action {
            border: 0; border-radius: 8px; padding: 8px 9px;
            background: transparent; color: #4f46e5; cursor: pointer;
            font-size: 12px; font-weight: 600; white-space: nowrap;
        }
        .text-action:hover:not(:disabled) { background: #eef2ff; }
        .text-action:disabled { opacity: .38; cursor: not-allowed; }
        .timeline-metrics {
            display: grid; grid-template-columns: 1.45fr repeat(3, minmax(0, 1fr));
            gap: 12px; margin-bottom: 14px;
        }
        .metric {
            min-width: 0; border: 1px solid var(--line); border-radius: var(--radius);
            background: #fff; box-shadow: var(--shadow); padding: 17px 17px 15px;
        }
        .metric-primary { background: linear-gradient(135deg, #4f46e5, #6366f1); color: #fff; border: 0; }
        .metric-label { font-size: 12px; color: var(--muted); font-weight: 600; }
        .metric-value { margin-top: 9px; line-height: 1.08; font-size: clamp(24px, 3vw, 32px); font-weight: 750; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
        .metric-unit { margin-top: 8px; font-size: 11px; color: #94a3b8; }
        .metric-primary .metric-label, .metric-primary .metric-unit { color: rgba(255,255,255,.82); }
        .selected-summary {
            background: #eef2ff; color: #3730a3; padding: 12px 18px;
            border-radius: 12px; border: 1px solid #c7d2fe; margin-bottom: 14px;
            display: flex; align-items: center; justify-content: space-between; gap: 12px;
        }
        .selected-summary-label { font-size: 12px; font-weight: 600; }
        .selected-summary-total { font-size: 26px; font-weight: 750; font-variant-numeric: tabular-nums; margin-top: 3px; }
        .selected-summary-total small { font-size: 12px; font-weight: 500; }
        .trend-panel {
            background: #fff; border: 1px solid var(--line);
            border-radius: var(--radius); box-shadow: var(--shadow);
            padding: 17px 18px 12px; margin-bottom: 17px;
        }
        .trend-header { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 16px; }
        .trend-header strong { font-size: 14px; margin-right: 12px; }
        .trend-header span, .trend-hint { color: var(--muted); font-size: 11px; }
        .trend-hint { white-space: nowrap; }
        .trend-scroll { overflow-x: auto; padding: 0 2px 5px; }
        .trend-bars {
            display: flex; align-items: stretch; gap: 9px;
            height: 150px; min-width: max-content; width: 100%;
        }
        .trend-bar-button {
            appearance: none; background: transparent; border: 0; padding: 0;
            display: flex; flex: 1 0 30px; min-width: 30px; max-width: 80px;
            flex-direction: column; align-items: center; cursor: pointer; color: var(--muted);
        }
        .trend-num { height: 22px; font-size: 10px; white-space: nowrap; font-variant-numeric: tabular-nums; }
        .trend-bar-track {
            display: flex; align-items: flex-end; flex: 1;
            width: 72%; min-width: 15px; border-radius: 5px 5px 0 0;
        }
        .trend-bar-fill {
            display: block; width: 100%; min-height: 3px; max-height: 100%;
            background: #a5b4fc; border-radius: 5px 5px 0 0;
            transition: height .2s ease, background .2s ease;
        }
        .trend-bar-button:hover .trend-bar-fill,
        .trend-bar-button.selected .trend-bar-fill { background: #4f46e5; }
        .trend-bar-button.selected .trend-day { color: #4338ca; font-weight: 700; }
        .trend-day { height: 22px; margin-top: 7px; font-size: 10px; white-space: nowrap; }
        .timeline-list-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0 3px 14px; }
        .timeline-list-title { display: flex; align-items: center; gap: 10px; }
        .timeline-list-title strong { font-size: 15px; }
        .timeline-list-title span { color: var(--muted); font-size: 12px; }
        .timeline-list-actions { display: flex; flex-wrap: wrap; gap: 5px; }
        .timeline-group { margin-bottom: 20px; }
        .timeline-date {
            top: -1px; position: sticky; z-index: 3;
            padding: 11px 10px; gap: 9px; margin: 0 0 6px;
            border: 1px solid #ebeff6; background: rgba(249,251,255,.96);
            backdrop-filter: blur(8px); border-radius: 10px;
        }
        .timeline-date.chosen { background: #eef2ff; border-color: #c7d2fe; }
        .day-check { display: flex; align-items: center; justify-content: center; padding: 2px; min-width: 25px; min-height: 28px; cursor: pointer; }
        .day-check input { accent-color: var(--primary); width: 17px; height: 17px; cursor: pointer; }
        .timeline-date-dot { width: 8px; height: 8px; background: var(--primary); box-shadow: 0 0 0 3px #e0e7ff; }
        .date-select-btn { border: 0; background: transparent; padding: 5px 2px; cursor: pointer; text-align: left; color: var(--ink); }
        .date-select-btn:disabled { cursor: default; }
        .timeline-date-count { background: #e9eef7; color: #607086; }
        .timeline-date-words { font-size: 12px; color: #4338ca; font-weight: 750; white-space: nowrap; }
        .timeline-date-line { background: #e6eaf1; }
        .timeline-items { border-left-color: #d9dff0; }
        .timeline-item {
            width: 100%; text-align: left; font-family: inherit;
            border-color: var(--line); border-radius: 12px; margin-bottom: 8px;
            box-shadow: var(--shadow); padding: 14px 16px; color: inherit;
        }
        .timeline-item:hover { border-color: #a5b4fc; }
        .timeline-item::before { background: #a5b4fc; }
        .timeline-item-title { color: var(--ink); font-size: 13.5px; }
        .timeline-item-words { color: #4f46e5; font-size: 12px; font-weight: 750; white-space: nowrap; }
        .timeline-footnote { color: #8591a3; font-size: 11px; line-height: 1.75; padding: 12px 3px; }
        .cat-badge { border-radius: 7px; border: none; white-space: nowrap; }
        .reader { padding: 30px 20px 70px; }
        .reader-inner { background: #fff; border: 1px solid var(--line); padding: 26px 34px; border-radius: 16px; box-shadow: var(--shadow); max-width: 870px; }
        .reader-bar { padding: 8px 16px; }
        .stats-table-scroll { overflow-x: auto; max-width: 100%; }
        .stats-table { min-width: 535px; }
        @media (max-width: 1024px) {
            .timeline-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .topbar { padding: 10px 14px; }
        }
        @media (max-width: 768px) {
            #app { height: 100dvh; }
            .sidebar { transition: transform .24s ease; padding-bottom: env(safe-area-inset-bottom,0px); }
            .sidebar.open { box-shadow: 5px 0 36px rgba(15,23,42,.20); }
            .main { min-width: 0; }
            .topbar {
                display: grid; grid-template-columns: 34px minmax(0, 1fr) auto auto;
                grid-template-rows: auto auto; gap: 9px; align-items: center;
                padding: calc(8px + env(safe-area-inset-top, 0px)) 10px 10px;
                min-height: 105px;
            }
            .topbar > .mobile-only { grid-column: 1; grid-row: 1; }
            .topbar > .brand { grid-column: 2; grid-row: 1; min-width: 0; }
            .brand span.brand-text { display: inline; font-size: 13px; }
            .topbar > .search-wrap { grid-column: 1 / -1; grid-row: 2; width: 100%; min-width: 0; }
            .topbar > .view-switch { grid-column: 3; grid-row: 1; }
            .topbar > .topbar-right { grid-column: 4; grid-row: 1; }
            .view-switch-btn { padding: 6px 10px; font-size: 11px; min-height: 32px; }
            .topbar-right .el-button { padding: 6px 9px; min-height: 33px; }
            .stat-mini { display: none; }
            .timeline-wrap { padding: 12px 10px calc(32px + env(safe-area-inset-bottom,0px)); }
            .timeline-toolbar { padding: 13px; gap: 12px; }
            .timeline-toolbar-row { gap: 10px; }
            .timeline-control { gap: 6px; }
            .control-caption { font-size: 11px; }
            .timeline-presets { gap: 5px; }
            .filter-pill { flex: 1 0 auto; padding: 8px 9px; min-height: 36px; }
            .timeline-range { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
            .date-field { flex-direction: column; align-items: stretch; gap: 3px; padding: 7px 10px; min-width: 0; }
            .date-field input { width: 100%; min-width: 0; max-width: none; font-size: 13px; }
            .timeline-range > .text-action { grid-column: 1 / -1; justify-self: start; }
            .timeline-metrics { gap: 8px; }
            .metric { padding: 13px 12px; border-radius: 12px; }
            .metric-value { font-size: 25px; }
            .metric-label { font-size: 11px; }
            .metric-unit { font-size: 10px; }
            .trend-panel { padding: 13px 12px 10px; }
            .trend-header { align-items: flex-start; }
            .trend-hint { display: none; }
            .trend-header strong { display: block; margin-bottom: 3px; }
            .trend-bar-button { flex-basis: 30px; }
            .timeline-list-heading { align-items: flex-start; }
            .timeline-list-title { display: block; }
            .timeline-list-title span { display: block; margin-top: 3px; }
            .timeline-date { gap: 7px; top: -1px; padding: 9px 7px; flex-wrap: wrap; }
            .timeline-date-text { font-size: 13px; }
            .timeline-date-words { font-size: 12px; }
            .timeline-date-line { display: none; }
            .timeline-items { padding-left: 13px; margin-left: 10px; }
            .timeline-item { padding: 11px; flex-wrap: wrap; gap: 7px; }
            .timeline-item::before { left: -20px; }
            .timeline-item-time { width: 38px; }
            .timeline-item-body { flex-basis: 50%; }
            .timeline-item-words { margin-left: auto; }
            .cat-badge { font-size: 11px; padding: 4px 7px; }
            .reader { padding: 12px 9px calc(30px + env(safe-area-inset-bottom,0px)); }
            .reader-inner { padding: 20px 15px; border-radius: 12px; }
            .reader-title { font-size: 21px; }
            .reader-body { font-size: 15px; line-height: 1.9; }
            .reader-bar { gap: 7px; min-height: 52px; }
            .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .stats-dialog .el-dialog__body { max-height: 75dvh; overflow-y: auto; padding: 13px; }
            .doc-list-wrap { padding: 12px 10px 30px; }
        }
        @media (max-width: 380px) {
            .brand span.brand-text { display: none; }
            .topbar { gap: 6px; }
            .view-switch-btn { padding: 6px 8px; }
            .timeline-control { width: 100%; }
            .timeline-control .el-radio-group { flex: 1; }
            .trend-header span { font-size: 10px; }
        }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; }
        }

    </style>

<style>
  #authGate{position:fixed;inset:0;z-index:9999;background:rgba(10,12,16,.93);display:none;align-items:center;justify-content:center;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  #authGate.show{display:flex}
  #authGate .card{width:min(92vw,340px);background:#161a22;border:1px solid #232a37;border-radius:14px;padding:22px 20px;display:flex;flex-direction:column;gap:10px;color:#e6e8ee}
  #authGate h2{margin:0 0 4px;font-size:16px}
  #authGate p{margin:0;font-size:12px;color:#8b93a7}
  #authGate input{background:#0f1116;border:1px solid #232a37;color:inherit;padding:10px 12px;border-radius:10px;font:inherit;outline:none;width:100%;box-sizing:border-box}
  #authGate input:focus{border-color:#3d6ea8}
  #authGate button{background:#2f6fd0;color:#fff;border:0;border-radius:10px;padding:10px;font:inherit;cursor:pointer}
  #authGate .err{color:#ff8b8b;font-size:12px;min-height:15px}
</style>
</head>

<body>
<div id="app" class="layout">

    <aside class="sidebar" :class="{ open: sidebarOpen }">

        <div class="sidebar-head">
            <div class="sidebar-head-row">
                <span class="sidebar-title">📂 分类目录</span>
                <span class="stat-btn" @click="openStats">📊 统计</span>
            </div>

            <div class="sidebar-summary" v-if="stats && stats.total">
                <div class="sum-item">
                    <div class="sum-value">{{ formatNum(stats.total.docs) }}</div>
                    <div class="sum-label">篇</div>
                </div>
                <div class="sum-item">
                    <div class="sum-value">{{ formatNum(stats.total.words) }}</div>
                    <div class="sum-label">总字</div>
                </div>
                <div class="sum-item">
                    <div class="sum-value">{{ formatNum(stats.total.avg_words) }}</div>
                    <div class="sum-label">均字</div>
                </div>
            </div>
        </div>

        <div class="sidebar-body">

            <div class="cat-item"
                 :class="{ active: !activeCat }"
                 @click="selectCategory(null, null)">
                <span class="cat-dot" style="background:#64748b"></span>
                <span class="cat-name">全部笔记</span>
                <span class="cat-count">
                    {{ totalDocs }}
                    <span class="avg" v-if="overallAvg > 0">
                        均{{ formatNum(overallAvg) }}
                    </span>
                </span>
            </div>

            <template v-for="cat in categoryTree" :key="cat.code">
                <div class="cat-item"
                     :class="{ active: activeCat === cat.code && !activeSub }"
                     @click="selectCategory(cat.code, null)">

                    <span class="cat-arrow"
                          :class="{ open: expandedCats[cat.code] }"
                          @click.stop="toggleCat(cat.code)">▶</span>

                    <span class="cat-dot"
                          :style="{ background: cat.color }"></span>

                    <span class="cat-name">
                        {{ cat.label }}
                        <small>{{ cat.code }}</small>
                    </span>

                    <span class="cat-count">
                        {{ cat.count }}
                        <span class="avg" v-if="cat.avg > 0">
                            均{{ formatNum(cat.avg) }}
                        </span>
                    </span>
                </div>

                <div class="subcat-list" v-show="expandedCats[cat.code]">
                    <div class="subcat-item"
                         v-for="sub in cat.children"
                         :key="sub.code"
                         :class="{ active: activeCat === cat.code && activeSub === sub.code }"
                         @click="selectCategory(cat.code, sub.code)">

                        <span>{{ sub.code }}</span>
                        <span v-if="sub.label" style="color:#94a3b8">· {{ sub.label }}</span>

                        <span class="subcat-stats">
                            <span>{{ sub.count }} 篇</span>
                            <span v-if="sub.avg > 0">均{{ formatNum(sub.avg) }}</span>
                        </span>
                    </div>
                </div>
            </template>

        </div>
    </aside>

    <div class="sidebar-mask"
         v-if="isMobile && sidebarOpen"
         @click="sidebarOpen = false"></div>

    <main class="main">

        <template v-if="view === 'list'">

            <div class="topbar">
                <el-button class="mobile-only" size="small" circle plain
                           aria-label="打开分类目录"
                           @click="sidebarOpen = true">☰</el-button>

                <div class="brand">
                    <span>📖</span>
                    <span class="brand-text">阅读模式</span>
                </div>

                <div class="search-wrap">
                    <el-input v-model="keyword" size="small"
                              placeholder="搜索标题或内容..."
                              clearable @keyup.enter="doSearch">
                        <template #prefix>
                            <span style="color:#94a3b8">🔍</span>
                        </template>
                    </el-input>
                </div>

                <div class="view-switch">
                    <button class="view-switch-btn"
                            :class="{ active: viewMode === 'card' }"
                            @click="viewMode = 'card'">
                        卡片
                    </button>
                    <button class="view-switch-btn"
                            :class="{ active: viewMode === 'timeline' }"
                            @click="viewMode = 'timeline'">
                        时间线
                    </button>
                </div>

                <div class="topbar-right">
                    <span class="stat-mini">
                        {{ filteredDocs.length }} / {{ totalDocs }} 篇
                    </span>
                    <el-button size="small" plain
                               @click="loadAll" :loading="loadingDocs">
                        刷新
                    </el-button>
                </div>
            </div>

            <!-- 卡片视图 -->
            <div v-if="viewMode === 'card'"
                 class="doc-list-wrap" ref="listWrap">

                <div v-if="loadingDocs" class="empty-hint">正在加载笔记...</div>

                <div v-else-if="groupedDocs.length === 0" class="empty-hint">
                    <div style="font-size:36px;margin-bottom:8px">🔍</div>
                    <div v-if="keyword">没有匹配「{{ keyword }}」的笔记</div>
                    <div v-else>还没有笔记</div>
                </div>

                <template v-else>
                    <div class="doc-group"
                         v-for="group in groupedDocs"
                         :key="group.code">

                        <div class="doc-group-head"
                             @click="toggleGroup(group.code)">

                            <span class="cat-arrow"
                                  :class="{ open: expandedGroups[group.code] }">▶</span>

                            <span class="doc-group-name">
                                <span :style="{ color: group.color }">●</span>
                                {{ group.label }}
                                <small style="color:#94a3b8;font-size:11px;font-weight:400">
                                    {{ group.code }}
                                </small>
                            </span>

                            <span class="doc-group-count">
                                {{ group.docs.length }} 篇
                                <span class="avg" v-if="group.avg > 0">
                                    均 {{ formatNum(group.avg) }} 字
                                </span>
                                <span class="avg">
                                    总 {{ formatNum(group.totalWords) }} 字
                                </span>
                            </span>

                            <span class="doc-group-line"></span>
                        </div>

                        <div v-show="expandedGroups[group.code]">
                            <div class="doc-card"
                                 v-for="doc in group.docs"
                                 :key="doc.id"
                                 @click="openDoc(doc)">

                                <div style="display:flex;align-items:flex-start;gap:8px">
                                    <!-- 卡片视图徽章：点击跳一级分类 -->
                                    <span class="cat-badge"
                                          :style="{ background: group.color + '20', color: group.color }"
                                          :title="`查看「${group.label}」全部文章`"
                                          @click.stop="jumpToCategory(group.code)">
                                        {{ group.code }}
                                    </span>

                                    <div class="doc-card-title" style="flex:1"
                                         v-html="highlight(getCleanTitle(doc))"></div>
                                </div>

                                <div class="doc-card-preview" v-if="getPreview(doc)"
                                     v-html="highlight(getPreview(doc))"></div>

                                <div class="doc-card-meta">
                                    <span v-if="doc.created_time">
                                        📅 {{ formatDate(doc.created_time) }}
                                    </span>
                                    <span class="meta-tag"
                                          v-if="getWordCount(doc) > 0">
                                        📝 {{ formatNum(getWordCount(doc)) }} 字
                                    </span>
                                    <span v-if="doc.is_pinned === 1" class="meta-tag"
                                          style="background:#fef3c7;color:#b45309">📌 置顶</span>
                                    <span v-if="doc.status === 'archive'" class="meta-tag"
                                          style="background:#f1f5f9">📦 归档</span>
                                </div>
                            </div>
                        </div>
                    </div>
                </template>
            </div>

            <!-- 时间线 · 日统计 / 日期区间 / 自由选日合计 -->
            <section v-else class="timeline-wrap" aria-label="文档时间线">
                <div class="timeline-inner">
                    <div class="timeline-toolbar">
                        <div class="timeline-toolbar-row">
                            <div class="timeline-control">
                                <span class="control-caption">日期依据</span>
                                <el-radio-group v-model="timelineField" size="small">
                                    <el-radio-button label="created">创建日期</el-radio-button>
                                    <el-radio-button label="update">更新日期</el-radio-button>
                                </el-radio-group>
                            </div>
                            <div class="timeline-control">
                                <span class="control-caption">排序</span>
                                <el-radio-group v-model="timelineOrder" size="small">
                                    <el-radio-button label="desc">从新到旧</el-radio-button>
                                    <el-radio-button label="asc">从旧到新</el-radio-button>
                                </el-radio-group>
                            </div>
                        </div>

                        <div class="timeline-presets" role="group" aria-label="快捷日期范围">
                            <button v-for="p in datePresets" :key="p.value" type="button"
                                    class="filter-pill"
                                    :class="{ active: rangePreset === p.value }"
                                    :aria-pressed="rangePreset === p.value"
                                    @click="applyDatePreset(p.value)">{{ p.label }}</button>
                        </div>

                        <div class="timeline-range">
                            <label class="date-field">
                                <span>开始日期</span>
                                <input type="date" v-model="dateStart" :max="dateEnd || undefined"
                                       @change="markCustomRange" aria-label="开始日期">
                            </label>
                            <label class="date-field">
                                <span>结束日期</span>
                                <input type="date" v-model="dateEnd" :min="dateStart || undefined"
                                       @change="markCustomRange" aria-label="结束日期">
                            </label>
                            <button class="text-action" type="button" @click="clearDateRange"
                                    :disabled="!dateStart && !dateEnd">清除日期</button>
                        </div>
                    </div>

                    <div v-if="!loadingDocs" class="timeline-metrics" aria-live="polite">
                        <div class="metric metric-primary">
                            <div class="metric-label">当前日期范围 · 合计字数</div>
                            <div class="metric-value">{{ formatExactNum(timelineSummary.words) }}</div>
                            <div class="metric-unit">字 · {{ timelineField === 'created' ? '按创建日期' : '按更新日期' }}</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">文档</div>
                            <div class="metric-value">{{ formatExactNum(timelineSummary.docs) }}</div>
                            <div class="metric-unit">篇有日期的文档</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">覆盖日期</div>
                            <div class="metric-value">{{ formatExactNum(timelineSummary.days) }}</div>
                            <div class="metric-unit">个有文档的日期</div>
                        </div>
                        <div class="metric">
                            <div class="metric-label">日均字数</div>
                            <div class="metric-value">{{ formatExactNum(timelineSummary.average) }}</div>
                            <div class="metric-unit">按有文档的日期平均</div>
                        </div>
                    </div>

                    <div v-if="selectedSummary.days > 0" class="selected-summary" aria-live="polite">
                        <div>
                            <div class="selected-summary-label">已选 {{ selectedSummary.days }} 天 · {{ selectedSummary.docs }} 篇文档</div>
                            <div class="selected-summary-total">{{ formatExactNum(selectedSummary.words) }} <small>字合计</small></div>
                        </div>
                        <button type="button" class="text-action" @click="clearSelectedDays">取消选择</button>
                    </div>

                    <div v-if="timelineChart.length && !loadingDocs" class="trend-panel">
                        <div class="trend-header">
                            <div>
                                <strong>每日字数趋势</strong>
                                <span>点击柱形可以选择日期并累计</span>
                            </div>
                            <span class="trend-hint">最多显示 42 个有文档日期</span>
                        </div>
                        <div class="trend-scroll">
                            <div class="trend-bars" role="group" aria-label="每日字数柱形图">
                                <button v-for="day in timelineChart" :key="day.key"
                                        type="button" class="trend-bar-button"
                                        :class="{ selected: isDaySelected(day.key) }"
                                        :aria-pressed="isDaySelected(day.key)"
                                        :title="day.key + ' · ' + formatExactNum(day.words) + ' 字 · ' + day.docs.length + ' 篇'"
                                        @click="toggleDaySelection(day.key)">
                                    <span class="trend-num">{{ shortNum(day.words) }}</span>
                                    <span class="trend-bar-track">
                                        <span class="trend-bar-fill" :style="{ height: trendHeight(day.words) + '%' }"></span>
                                    </span>
                                    <span class="trend-day">{{ day.key.slice(5) }}</span>
                                </button>
                            </div>
                        </div>
                    </div>

                    <div v-if="timelineGroups.length && !loadingDocs" class="timeline-list-heading">
                        <div class="timeline-list-title">
                            <strong>按日浏览</strong>
                            <span>{{ timelineSummary.days }} 天 · {{ timelineSummary.docs }} 篇</span>
                        </div>
                        <div class="timeline-list-actions">
                            <button class="text-action" type="button" @click="selectVisibleDays">全选当前日期</button>
                            <button class="text-action" type="button" :disabled="!selectedDayKeys.length"
                                    @click="clearSelectedDays">清空选择</button>
                        </div>
                    </div>

                    <div v-if="loadingDocs" class="empty-hint">正在加载笔记...</div>
                    <div v-else-if="!timelineGroups.length" class="empty-hint">
                        <div style="font-size:30px;margin-bottom:8px">暂无结果</div>
                        <p>选定日期范围内没有匹配的文档，可尝试扩大范围或清除筛选。</p>
                    </div>

                    <template v-else>
                        <section class="timeline-group" v-for="group in timelineGroups" :key="group.key">
                            <div class="timeline-date" :class="{ chosen: isDaySelected(group.key) }">
                                <label class="day-check" :title="group.undated ? '无日期的文档无法按天选择' : '选择此日期'">
                                    <input type="checkbox" :checked="isDaySelected(group.key)"
                                           :disabled="group.undated"
                                           @change="toggleDaySelection(group.key)"
                                           :aria-label="'选择 ' + group.label">
                                </label>
                                <span class="timeline-date-dot"></span>
                                <button type="button" class="timeline-date-text date-select-btn"
                                        @click="toggleDaySelection(group.key)" :disabled="group.undated">
                                    {{ group.label }}
                                </button>
                                <span class="timeline-date-count">{{ group.docs.length }} 篇</span>
                                <span class="timeline-date-words">{{ formatExactNum(group.words) }} 字</span>
                                <span class="timeline-date-line"></span>
                            </div>

                            <div class="timeline-items">
                                <button type="button" class="timeline-item" v-for="d in group.docs"
                                        :key="d.raw.id" @click="openDoc(d.raw)">
                                    <span class="timeline-item-time">{{ group.undated ? '--:--' : formatTimeOfDay(d.timestamp) }}</span>
                                    <span class="cat-badge" :style="badgeStyle(d.cat)"
                                          :title="'查看该分类全部文章'"
                                          @click.stop="jumpToCategory((d.cat.l1 || '') + ' ' + (d.cat.l2 || ''))">
                                        {{ d.cat.l1 }}{{ d.cat.l2 ? ' ' + d.cat.l2 : '' }}
                                    </span>
                                    <span class="timeline-item-body">
                                        <span class="timeline-item-title" v-html="highlight(d.cat.clean)"></span>
                                        <span class="timeline-item-preview" v-if="d.previewText"
                                              v-html="highlight(d.previewText)"></span>
                                        <span class="timeline-item-meta" v-if="d.raw.is_pinned === 1 || d.raw.status === 'archive'">
                                            <span v-if="d.raw.is_pinned === 1">置顶</span>
                                            <span v-if="d.raw.status === 'archive'">归档</span>
                                        </span>
                                    </span>
                                    <span class="timeline-item-words">{{ formatExactNum(d.words) }} 字</span>
                                </button>
                            </div>
                        </section>
                    </template>
                    <p class="timeline-footnote" v-if="!loadingDocs">
                        统计口径：根据文档{{ timelineField === 'created' ? '创建' : '更新' }}日期分组，对文档列表接口返回的
                        word_count 当前字数求和（缺失时按 0 计算）；并非历史每日新增字数。
                        <span v-if="timelineField === 'update'">更新日期缺失时回退到创建日期。</span>
                        <span v-if="timelineMissingDates">另有 {{ timelineMissingDates }} 篇未标日期文档，未计入日期字数合计。</span>
                    </p>
                </div>
            </section>

        </template>

        <template v-else>

            <div class="reader-bar">
                <el-button class="mobile-only" size="small" circle plain
                           aria-label="打开分类目录"
                           @click="sidebarOpen = true">☰</el-button>
                <el-button size="small" circle plain @click="closeReader">←</el-button>
                <span class="reader-bar-title">
                    {{ currentDoc ? getCleanTitle(currentDoc) : '加载中...' }}
                </span>
                <el-button size="small" plain @click="copyLink" title="复制链接">🔗</el-button>
                <el-button size="small" plain
                           @click="currentDoc && openDoc(currentDoc, true)" :disabled="!currentDoc" title="重新加载">🔄</el-button>
            </div>

            <div class="reader" ref="readerWrap">
                <div class="reader-inner">

                    <div v-if="loadingContent" class="empty-hint">正在加载内容...</div>

                    <template v-else-if="currentDoc">
                        <div class="reader-head">
                            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                                <!-- 阅读视图徽章：点击返回列表并筛选该分类 -->
                                <span class="cat-badge"
                                      :style="{ background: currentCat.color + '20', color: currentCat.color }"
                                      :title="`查看「${currentCat.code}」全部文章`"
                                      @click="jumpToCategory(currentCat.code)">
                                    {{ currentCat.code }}
                                </span>

                                <span v-if="currentDoc.is_pinned === 1"
                                      style="font-size:11px;color:#b45309">📌 置顶</span>
                                <span v-if="currentDoc.status === 'archive'"
                                      style="font-size:11px;color:#64748b">📦 归档</span>
                            </div>

                            <h1 class="reader-title">{{ getCleanTitle(currentDoc) }}</h1>

                            <div class="reader-meta">
                                <span class="reader-meta-item" v-if="currentDoc.created_time">
                                    🕐 创建于 {{ formatDateTime(currentDoc.created_time) }}
                                </span>
                                <span class="reader-meta-item" v-if="currentDoc.update_time">
                                    ✏️ 更新于 {{ formatDateTime(currentDoc.update_time) }}
                                </span>
                                <span class="reader-meta-item">
                                    📝 {{ formatNum(currentContent.length) }} 字符
                                </span>
                                <span class="reader-meta-item">
                                    ☕ 约 {{ readingTime }} 分钟
                                </span>
                            </div>
                        </div>

                        <div class="reader-body" v-html="renderedHtml"></div>

                        <div v-if="!renderedHtml && currentContent"
                             style="margin-top:14px;padding:12px;
                                    background:#fef3c7;border-radius:6px;
                                    font-size:12px;color:#92400e">
                            ⚠️ 渲染为空，原文预览：
                            <pre style="white-space:pre-wrap;margin-top:8px;
                                        font-size:12px;color:#1e293b">{{ currentContent.slice(0, 500) }}</pre>
                        </div>
                    </template>

                    <div v-else class="empty-hint">
                        未找到笔记「{{ missingId }}」
                    </div>
                </div>
            </div>
        </template>

    </main>

    <el-dialog v-model="statsDialog" title="知识库统计"
               :width="isMobile ? 'calc(100vw - 24px)' : '640px'" top="5vh" class="stats-dialog">

        <div v-if="statsLoading" class="empty-hint">正在加载统计...</div>
        <div v-else-if="!stats" class="empty-hint">暂无统计数据</div>

        <div v-else class="stats-panel">

            <div class="stats-grid">
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.total.docs) }}</div>
                    <div class="l">总篇数</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.total.words) }}</div>
                    <div class="l">总字数</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.total.avg_words) }}</div>
                    <div class="l">平均字数</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.total.pinned) }}</div>
                    <div class="l">置顶</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.total.active) }}</div>
                    <div class="l">活跃</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.total.archived) }}</div>
                    <div class="l">归档</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.time.today) }}</div>
                    <div class="l">今日新增</div>
                </div>
                <div class="stats-cell">
                    <div class="v">{{ formatNum(stats.time.week) }}</div>
                    <div class="l">本周新增</div>
                </div>
            </div>

            <div class="stats-section-title">按分类统计</div>
            <div class="stats-table-scroll"><table class="stats-table">
                <thead>
                <tr>
                    <th>分类</th>
                    <th class="num">篇数</th>
                    <th class="num">总字数</th>
                    <th class="num">平均字数</th>
                    <th class="num">最长</th>
                    <th class="num">最短</th>
                </tr>
                </thead>
                <tbody>
                <tr v-for="c in stats.categories" :key="c.code">
                    <td>
                        <span :style="{ color: (L1_META[c.code]||{}).color || '#64748b' }">●</span>
                        {{ (L1_META[c.code]||{}).label || c.code }}
                        <small style="color:#94a3b8">{{ c.code }}</small>
                    </td>
                    <td class="num">{{ c.count }}</td>
                    <td class="num">{{ formatNum(c.words) }}</td>
                    <td class="num" style="font-weight:600;color:#1d4ed8">
                        {{ formatNum(c.avg) }}
                    </td>
                    <td class="num">{{ formatNum(c.max) }}</td>
                    <td class="num">{{ formatNum(c.min) }}</td>
                </tr>
                </tbody>
            </table></div>

            <div class="stats-section-title">最长的 5 篇</div>
            <div class="stats-top-list">
                <div class="stats-top-item"
                     v-for="d in stats.top_longest"
                     :key="'L'+d.id"
                     @click="openFromStats(d.id)">
                    <span class="t">{{ d.title }}</span>
                    <span class="w">{{ formatNum(d.words) }} 字</span>
                </div>
            </div>

        </div>
    </el-dialog>

</div>

<script>
const { createApp, ref, computed, watch, nextTick, onMounted, onUnmounted } = Vue;

const API_BASE = (window.API_BASE || '').replace(/\/$/, '');
const api = path => /^https?:\/\//i.test(path) ? path : API_BASE + path;

/* =========================================================
   Markdown → HTML
   ========================================================= */
const escapeHtml = s =>
    String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');

function markdownToHtml(src) {
    let text = String(src || '');
    const codeBlocks = [];
    text = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g,
        (m, lang, code) => {
            codeBlocks.push({ lang: lang || '', code });
            return `\u0000CB${codeBlocks.length - 1}\u0000`;
        });

    text = escapeHtml(text);
    text = text.replace(/`([^`\n]+)`/g, '<code>$1</code>');

    text = text.replace(/^######[ \t]+(.+)$/gm, '<h6>$1</h6>');
    text = text.replace(/^#####[ \t]+(.+)$/gm,  '<h5>$1</h5>');
    text = text.replace(/^####[ \t]+(.+)$/gm,   '<h4>$1</h4>');
    text = text.replace(/^###[ \t]+(.+)$/gm,    '<h3>$1</h3>');
    text = text.replace(/^##[ \t]+(.+)$/gm,     '<h2>$1</h2>');
    text = text.replace(/^#[ \t]+(.+)$/gm,      '<h1>$1</h1>');

    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/__([^_\n]+)__/g,     '<strong>$1</strong>');
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    text = text.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

    text = text.replace(/^&gt;[ \t]?(.*)$/gm, '<blockquote>$1</blockquote>');
    text = text.replace(/(<blockquote>[\s\S]*?<\/blockquote>\n?)+/g, m =>
        '<blockquote>' +
        m.replace(/<\/?blockquote>/g, '').replace(/\n/g, '<br>') +
        '</blockquote>\n'
    );

    // Markdown 转 HTML 时，仅允许常见安全协议和相对路径。
    const safeUrl = input => {
        const u = String(input || '').replace(/&amp;/g, '&').trim();
        if (!/^(https?:\/\/|mailto:|\/(?!\/)|\.\.?\/|#)/i.test(u)) return '';
        return escapeHtml(u).replace(/'/g, '&#39;');
    };
    text = text.replace(
        /!\[([^\]]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g,
        (m, alt, url, title) => {
            const safe = safeUrl(url);
            if (!safe || /^mailto:/i.test(url)) return alt;
            return `<img src="${safe}" alt="${alt}"${title ? ` title="${title}"` : ''} loading="lazy">`;
        }
    );
    text = text.replace(
        /\[([^\]]+)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g,
        (m, label, url, title) => {
            const safe = safeUrl(url);
            if (!safe) return label;
            return `<a href="${safe}" target="_blank" rel="noopener noreferrer"${title ? ` title="${title}"` : ''}>${label}</a>`;
        }
    );

    text = text.replace(/^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$/gm, '<hr>');

    text = text.replace(/^[ \t]*[-*+][ \t]+(.+)$/gm, '<li>$1</li>');
    text = text.replace(/(?:^|\n)((?:[ \t]*<li>.*<\/li>[ \t]*\n?)+)/g,
        (m, g) => '\n<ul>' + g.replace(/^\s+/gm, '') + '</ul>\n');

    text = text.replace(/^[ \t]*\d+\.[ \t]+(.+)$/gm, '<oli>$1</oli>');
    text = text.replace(/(?:^|\n)((?:[ \t]*<oli>.*<\/oli>[ \t]*\n?)+)/g,
        (m, g) => '\n<ol>' +
            g.replace(/^\s+/gm, '')
             .replace(/<oli>/g, '<li>')
             .replace(/<\/oli>/g, '</li>') +
            '</ol>\n'
    );

    text = text.split(/\n{2,}/).map(b => {
        const p = b.trim();
        if (!p) return '';
        if (/^<(h\d|ul|ol|blockquote|pre|hr|li|table|img|p)/i.test(p)) return p;
        return '<p>' + p.replace(/\n/g, '<br>') + '</p>';
    }).join('\n');

    text = text.replace(/\u0000CB(\d+)\u0000/g, (m, idx) => {
        const cb = codeBlocks[parseInt(idx, 10)];
        if (!cb) return '';
        const la = cb.lang ? ` class="language-${cb.lang}"` : '';
        return `<pre><code${la}>${escapeHtml(cb.code)}</code></pre>`;
    });

    return text;
}

/* =========================================================
   分类元数据
   ========================================================= */
const L1_META = {
    pol:  { label: '政治',  color: '#dc2626' },
    his:  { label: '历史',  color: '#b45309' },
    mil:  { label: '军事',  color: '#7c2d12' },
    eco:  { label: '经济',  color: '#0891b2' },
    soc:  { label: '社会',  color: '#7c3aed' },
    psy:  { label: '心理',  color: '#db2777' },
    know: { label: '方法',  color: '#059669' },
    exe:  { label: '运动',  color: '#ea580c' },
    med:  { label: '医疗',  color: '#16a34a' },
    out:  { label: '户外',  color: '#65a30d' },
    tec:  { label: '技术',  color: '#2563eb' },
    fan:  { label: '网文',  color: '#a21caf' },
    cul:  { label: '文艺',  color: '#9333ea' },
    dai:  { label: '记录',  color: '#64748b' },
    adu:  { label: '成人',  color: '#be185d' },
    unk:  { label: '未分类', color: '#94a3b8' },
};
const L1_CODES = new Set(Object.keys(L1_META));

const L2_LABEL = {
    pow:'权力', prop:'宣传', ctrl:'管控', gov:'治理',
    law:'法律', econ:'政治经济', welf:'福利', ethn:'民族',
    intl:'外交', hist:'党史', thy:'理论', misc:'其他',
    anc:'古代', qing:'晚清', rep:'民国', prc:'建国后', meth:'史学',
    strat:'战略', war:'作战', tech:'武器',
    fin:'金融', inv:'投资', biz:'商业', macro:'宏观',
    class:'阶层', media:'传播', demo:'人口', relig:'宗教', labor:'劳动',
    mind:'心理', rel:'关系', group:'群体',
    model:'模型', decide:'决策', pkm:'知识库', fake:'纠错',
    str:'力量', endu:'耐力', plan:'计划', sci:'科学', recov:'恢复',
    oral:'口腔', rehab:'康复', sleep:'睡眠', nutri:'营养', drug:'药物',
    hike:'徒步', moto:'摩托', camp:'露营', bike:'骑行', auto:'汽车',
    ai:'AI', agent:'智能体', cost:'成本', dev:'开发', net:'网络',
    set:'设定', char:'角色', plot:'剧情', pick:'选书',
    gen:'生成', mkt:'市场',
    book:'书评', film:'影视', phil:'哲学',
    log:'日记', train:'训练', buy:'购买',
    lib:'整理', ana:'分析',
};

const parseDocIdFromUrl = () => {
    const p = new URLSearchParams(window.location.search);
    return p.get('doc') || '';
};
const buildDocUrl = id => {
    const u = new URL(window.location.href);
    u.searchParams.set('doc', id);
    return u.pathname + u.search + u.hash;
};
const buildListUrl = () => {
    const u = new URL(window.location.href);
    u.searchParams.delete('doc');
    return u.pathname + u.search + u.hash;
};

const pad2 = n => String(n).padStart(2, '0');
const dateKey = date =>
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
const startOfDay = date => {
    const d = new Date(date);
    d.setHours(0, 0, 0, 0);
    return d;
};

/** 将 UNIX 秒/毫秒、ISO 字符串及 YYYY-MM-DD 转为本地时间戳（毫秒）。 */
const parseTimestamp = input => {
    if (input === null || input === undefined || input === '') return 0;
    if (input instanceof Date) return Number.isFinite(input.getTime()) ? input.getTime() : 0;
    const value = String(input).trim();
    if (/^\d+(?:\.\d+)?$/.test(value)) {
        const n = Number(value);
        if (!Number.isFinite(n) || n <= 0) return 0;
        const ms = n < 1e11 ? n * 1000 : n;
        return Number.isFinite(new Date(ms).getTime()) ? ms : 0;
    }
    const pureDay = value.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (pureDay) {
        const d = new Date(Number(pureDay[1]), Number(pureDay[2]) - 1, Number(pureDay[3]));
        return Number.isFinite(d.getTime()) ? d.getTime() : 0;
    }
    const ms = Date.parse(value.replace(/^(\d{4}-\d{2}-\d{2}) (\d)/, '$1T$2'));
    return Number.isFinite(ms) ? ms : 0;
};
const formatExactNumber = n => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })
    .format(Math.max(0, Math.round(Number(n) || 0)));


/* =========================================================
   App
   ========================================================= */
const app = createApp({
    setup() {

        const documents   = ref([]);
        const loadingDocs = ref(false);
        const keyword     = ref('');
        const activeCat   = ref(null);
        const activeSub   = ref(null);

        const expandedCats   = ref({});
        const expandedGroups = ref({});

        const view        = ref('list');
        const viewMode    = ref('card');
        const sidebarOpen = ref(false);

        const timelineField = ref('created');
        const timelineOrder = ref('desc');
        const rangePreset = ref('all');
        const dateStart = ref('');
        const dateEnd = ref('');
        const selectedDayKeys = ref([]);
        const datePresets = [
            { value: 'all', label: '全部时间' },
            { value: '7d', label: '近 7 天' },
            { value: '30d', label: '近 30 天' },
            { value: 'month', label: '本月' },
            { value: 'custom', label: '自定义' },
        ];

        const currentDoc     = ref(null);
        const currentContent = ref('');
        const renderedHtml   = ref('');
        const loadingContent = ref(false);
        const missingId      = ref('');

        const stats         = ref(null);
        const statsLoading  = ref(false);
        const statsDialog   = ref(false);

        const isMobile = ref(window.innerWidth <= 768);
        const listWrap   = ref(null);
        const readerWrap = ref(null);

        /* ---------- 分类解析 ---------- */
        const parseCategory = title => {
            const t = String(title || '').trim();
            let m = t.match(/^\[([a-z]{2,8})\s+([a-z]{2,8})\]\s*(.*)$/i);
            if (m) return { l1: m[1].toLowerCase(), l2: m[2].toLowerCase(),
                            clean: m[3].trim() || '未命名笔记' };
            m = t.match(/^([a-z]{2,8})\s+([a-z]{2,8})\s+(.+)$/i);
            if (m && L1_CODES.has(m[1].toLowerCase())) {
                return { l1: m[1].toLowerCase(), l2: m[2].toLowerCase(),
                         clean: m[3].trim() };
            }
            return { l1: 'unk', l2: '', clean: t || '未命名笔记' };
        };

        const getCleanTitle = doc => {
            if (!doc) return '';
            const cat = parseCategory(doc.title);
            return cat.clean || doc.title || doc.id || '未命名笔记';
        };

        const getPreview = doc => {
            const raw = doc.preview || doc.summary || doc.snippet ||
                        doc.content_preview || '';
            if (!raw) return '';
            return String(raw)
                .replace(/```[\s\S]*?```/g, ' ')
                .replace(/[#>*_`~]/g, '')
                .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
                .replace(/\s+/g, ' ')
                .trim();
        };

        const getWordCount = doc => {
            if (!doc) return 0;
            const wc = Number(doc.word_count ?? doc.wordCount ?? doc.words ?? 0);
            return Number.isFinite(wc) && wc > 0 ? Math.round(wc) : 0;
        };

        const badgeStyle = cat => {
            const meta = L1_META[cat.l1] || L1_META.unk;
            return { background: meta.color + '20', color: meta.color };
        };

        const statsMap = computed(() => {
            const m = {};
            if (!stats.value || !stats.value.categories) return m;
            for (const c of stats.value.categories) {
                m[c.code] = {
                    avg: c.avg, count: c.count, words: c.words,
                    max: c.max, min: c.min, children: {},
                };
                for (const sub of (c.children || [])) {
                    m[c.code].children[sub.code] = {
                        avg: sub.avg, count: sub.count, words: sub.words,
                    };
                }
            }
            return m;
        });

        const decoratedDocs = computed(() =>
            documents.value.map(doc => {
                const cat = parseCategory(doc.title);
                return {
                    raw: doc, cat,
                    previewText: getPreview(doc),
                    words: getWordCount(doc),
                };
            })
        );

        const filteredDocs = computed(() => {
            let list = decoratedDocs.value;
            if (activeCat.value) {
                list = list.filter(d => d.cat.l1 === activeCat.value);
                if (activeSub.value) {
                    list = list.filter(d => d.cat.l2 === activeSub.value);
                }
            }
            const q = keyword.value.trim().toLowerCase();
            if (q) {
                list = list.filter(d => {
                    const t = (d.cat.clean || '').toLowerCase();
                    const p = (d.previewText || '').toLowerCase();
                    return t.includes(q) || p.includes(q);
                });
            }
            return list;
        });

        const groupedDocs = computed(() => {
            const groups = {};
            for (const d of filteredDocs.value) {
                const code = d.cat.l1 || 'unk';
                if (!groups[code]) {
                    const meta = L1_META[code] || L1_META.unk;
                    groups[code] = {
                        code, label: meta.label, color: meta.color,
                        docs: [], totalWords: 0,
                    };
                }
                groups[code].docs.push(d.raw);
                groups[code].totalWords += d.words || 0;
            }
            return Object.values(groups).map(g => ({
                ...g,
                avg: g.docs.length
                    ? Math.round(g.totalWords / g.docs.length)
                    : 0,
            })).sort((a, b) => b.docs.length - a.docs.length);
        });

        // 新出现的分类默认展开；不覆盖用户手动折叠的状态。
        watch(groupedDocs, groups => {
            const next = { ...expandedGroups.value };
            let changed = false;
            for (const group of groups) {
                if (!(group.code in next)) {
                    next[group.code] = true;
                    changed = true;
                }
            }
            if (changed) expandedGroups.value = next;
        }, { immediate: true });

        const categoryTree = computed(() => {
            const map = {};
            for (const d of decoratedDocs.value) {
                const code = d.cat.l1 || 'unk';
                if (!map[code]) {
                    const meta = L1_META[code] || L1_META.unk;
                    map[code] = {
                        code, label: meta.label, color: meta.color,
                        count: 0, totalWords: 0, children: {},
                    };
                }
                map[code].count += 1;
                map[code].totalWords += d.words || 0;

                const sub = d.cat.l2;
                if (sub) {
                    if (!map[code].children[sub]) {
                        map[code].children[sub] = {
                            code: sub,
                            label: L2_LABEL[sub] || sub,
                            count: 0, totalWords: 0,
                        };
                    }
                    map[code].children[sub].count += 1;
                    map[code].children[sub].totalWords += d.words || 0;
                }
            }

            return Object.values(map).map(cat => {
                const s = statsMap.value[cat.code];
                const avg = s && s.avg
                    ? Math.round(s.avg)
                    : (cat.count ? Math.round(cat.totalWords / cat.count) : 0);

                const children = Object.values(cat.children).map(sub => {
                    const ss = s && s.children && s.children[sub.code];
                    const subAvg = ss && ss.avg
                        ? Math.round(ss.avg)
                        : (sub.count
                            ? Math.round(sub.totalWords / sub.count)
                            : 0);
                    return { ...sub, avg: subAvg };
                }).sort((a, b) => b.count - a.count);

                return {
                    code: cat.code, label: cat.label, color: cat.color,
                    count: cat.count, totalWords: cat.totalWords,
                    avg, children,
                };
            }).sort((a, b) => b.count - a.count);
        });

        /* ---------- 时间线：统一过滤后的每日汇总、区间与多日勾选 ---------- */
        const getDocTimestamp = (doc, field = timelineField.value) => {
            if (!doc) return 0;
            const created = doc.created_time ?? doc.create_time ?? doc.created_at;
            if (field === 'created') return parseTimestamp(created);
            const updated = [doc.update_time, doc.updated_time, doc.modified_time, doc.updated_at]
                .find(value => parseTimestamp(value) > 0);
            return parseTimestamp(updated ?? created);
        };

        const applyDatePreset = preset => {
            rangePreset.value = preset;
            if (preset === 'custom') return;
            if (preset === 'all') {
                dateStart.value = '';
                dateEnd.value = '';
                return;
            }
            const today = startOfDay(new Date());
            dateEnd.value = dateKey(today);
            if (preset === 'month') {
                dateStart.value = dateKey(new Date(today.getFullYear(), today.getMonth(), 1));
            } else {
                const days = preset === '7d' ? 7 : 30;
                const from = new Date(today);
                from.setDate(from.getDate() - days + 1);
                dateStart.value = dateKey(from);
            }
        };

        const markCustomRange = () => {
            rangePreset.value = 'custom';
            if (dateStart.value && dateEnd.value && dateStart.value > dateEnd.value) {
                const tmp = dateStart.value;
                dateStart.value = dateEnd.value;
                dateEnd.value = tmp;
            }
        };
        const clearDateRange = () => applyDatePreset('all');

        const datedDocs = computed(() => filteredDocs.value.map(d => ({
            ...d,
            timestamp: getDocTimestamp(d.raw),
        })));

        const timelineMissingDates = computed(() =>
            datedDocs.value.filter(d => !d.timestamp).length
        );

        const timelineGroups = computed(() => {
            const groups = new Map();
            const now = startOfDay(new Date());
            const yesterday = new Date(now);
            yesterday.setDate(yesterday.getDate() - 1);
            for (const d of datedDocs.value) {
                const valid = d.timestamp > 0;
                const day = valid ? new Date(d.timestamp) : null;
                const key = day ? dateKey(day) : 'undated';
                if (valid && dateStart.value && key < dateStart.value) continue;
                if (valid && dateEnd.value && key > dateEnd.value) continue;
                // 输入了日期区间时，不把没有日期的文档混入统计。
                if (!valid && (dateStart.value || dateEnd.value)) continue;
                if (!groups.has(key)) {
                    groups.set(key, {
                        key,
                        date: day,
                        undated: !valid,
                        label: day ? humanDate(day, now, yesterday) : '未标日期',
                        docs: [],
                        words: 0,
                    });
                }
                const group = groups.get(key);
                group.docs.push(d);
                group.words += d.words;
            }
            const list = Array.from(groups.values());
            list.sort((a, b) => {
                if (a.undated) return 1;
                if (b.undated) return -1;
                return timelineOrder.value === 'asc'
                    ? a.key.localeCompare(b.key) : b.key.localeCompare(a.key);
            });
            for (const g of list) {
                g.docs.sort((a, b) => timelineOrder.value === 'asc'
                    ? a.timestamp - b.timestamp : b.timestamp - a.timestamp);
            }
            return list;
        });

        const timelineTotal = computed(() => timelineGroups.value.reduce(
            (n, g) => n + g.docs.length, 0
        ));
        const datedGroups = computed(() => timelineGroups.value.filter(g => !g.undated));
        const timelineSummary = computed(() => {
            const groups = datedGroups.value;
            const docs = groups.reduce((n, g) => n + g.docs.length, 0);
            const words = groups.reduce((n, g) => n + g.words, 0);
            return {
                docs, words, days: groups.length,
                average: groups.length ? Math.round(words / groups.length) : 0,
            };
        });
        const visibleDayKeys = computed(() => datedGroups.value.map(g => g.key));
        const selectedSummary = computed(() => {
            const keys = new Set(selectedDayKeys.value);
            const picked = datedGroups.value.filter(g => keys.has(g.key));
            return {
                days: picked.length,
                docs: picked.reduce((n, g) => n + g.docs.length, 0),
                words: picked.reduce((n, g) => n + g.words, 0),
            };
        });
        const toggleDaySelection = key => {
            if (key === 'undated') return;
            const values = new Set(selectedDayKeys.value);
            if (values.has(key)) values.delete(key);
            else values.add(key);
            selectedDayKeys.value = [...values];
        };
        const isDaySelected = key => selectedDayKeys.value.includes(key);
        const selectVisibleDays = () => { selectedDayKeys.value = [...visibleDayKeys.value]; };
        const clearSelectedDays = () => { selectedDayKeys.value = []; };
        watch([timelineField, activeCat, activeSub, keyword, dateStart, dateEnd],
              clearSelectedDays);

        const timelineChart = computed(() => {
            const chronological = datedGroups.value.slice(0, 42);
            return chronological.sort((a, b) => a.key.localeCompare(b.key));
        });
        const trendMax = computed(() =>
            Math.max(1, ...timelineChart.value.map(g => g.words))
        );
        const trendHeight = words => Math.max(3,
            Math.round(Math.max(0, Number(words) || 0) / trendMax.value * 100)
        );
        const shortNum = n => {
            const v = Number(n) || 0;
            if (v >= 10000) return (v / 10000).toFixed(1) + '万';
            if (v >= 1000) return (v / 1000).toFixed(1) + '千';
            return String(v);
        };
        const formatExactNum = formatExactNumber;

        const humanDate = (date, today, yesterday) => {
            const key = dateKey(date);
            const todayKey = dateKey(today);
            const yesterdayKey = dateKey(yesterday);

            if (key === todayKey) return '今天';
            if (key === yesterdayKey) return '昨天';

            const names = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
            const y = date.getFullYear();
            const m = date.getMonth() + 1;
            const d = date.getDate();

            const weekAgo = new Date(today);
            weekAgo.setDate(weekAgo.getDate() - 6);
            if (date >= weekAgo) {
                return `${m}月${d}日 ${names[date.getDay()]}`;
            }
            if (y === today.getFullYear()) {
                return `${m}月${d}日`;
            }
            return `${y}年${m}月${d}日`;
        };

        /* ---------- 其他计算 ---------- */
        const totalDocs  = computed(() => documents.value.length);

        const overallAvg = computed(() => {
            if (stats.value && stats.value.total) {
                return Math.round(stats.value.total.avg_words || 0);
            }
            if (!totalDocs.value) return 0;
            const sum = decoratedDocs.value.reduce((s, d) => s + (d.words || 0), 0);
            return Math.round(sum / totalDocs.value);
        });

        const readingTime = computed(() =>
            Math.max(1, Math.ceil(currentContent.value.length / 500))
        );

        const currentCat = computed(() => {
            if (!currentDoc.value) {
                return { code: 'unk', label: '未分类', color: '#94a3b8' };
            }
            const cat = parseCategory(currentDoc.value.title);
            const meta = L1_META[cat.l1] || L1_META.unk;
            return {
                code: cat.l2 ? `${cat.l1} ${cat.l2}` : cat.l1,
                label: meta.label,
                color: meta.color,
            };
        });

        /* ---------- 工具 ---------- */
        const formatNum = n => {
            n = Number(n) || 0;
            if (n >= 10000) return (n / 10000).toFixed(1) + 'w';
            if (n >= 1000)  return (n / 1000).toFixed(1) + 'k';
            return String(n);
        };
        const formatDate = ts => {
            const ms = parseTimestamp(ts);
            return ms ? dateKey(new Date(ms)) : '';
        };
        const formatDateTime = ts => {
            const ms = parseTimestamp(ts);
            if (!ms) return '';
            const d = new Date(ms);
            return `${dateKey(d)} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
        };
        const formatTimeOfDay = ts => {
            const ms = parseTimestamp(ts);
            if (!ms) return '';
            const d = new Date(ms);
            return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
        };
        const escapeReg = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const highlight = text => {
            const q = keyword.value.trim();
            if (!q) return escapeHtml(text);
            const safe = escapeHtml(text);
            const re = new RegExp(`(${escapeReg(escapeHtml(q))})`, 'gi');
            return safe.replace(re, '<span class="hl">$1</span>');
        };

        /* ---------- 交互 ---------- */
        const toggleCat = code => {
            expandedCats.value = {
                ...expandedCats.value,
                [code]: !expandedCats.value[code],
            };
        };
        const toggleGroup = code => {
            expandedGroups.value = {
                ...expandedGroups.value,
                [code]: !expandedGroups.value[code],
            };
        };

        /* ★ 核心修改：点击侧栏分类 → 强制回到列表目录，不再被文章盖住 ★ */
        const selectCategory = (l1, l2) => {
            activeCat.value = l1;
            activeSub.value = l2;

            // 展开当前一级分类（如果点的是二级分类，这里也会保证一级展开）
            if (l1 && !expandedCats.value[l1]) {
                expandedCats.value = { ...expandedCats.value, [l1]: true };
            }
            // 如果点的是“全部笔记”，把当前分类的展开也保留，不做额外处理

            // 若正在阅读文章，先退出阅读视图，直接显示目录列表
            if (view.value !== 'list') {
                closeReader();          // 内部会同步 URL（去掉 ?doc=）
            }

            // 移动端点击后收起抽屉
            if (isMobile.value) sidebarOpen.value = false;

            // 列表滚回顶部，让用户看到新选中的分类
            nextTick(() => {
                const listEl = document.querySelector('.doc-list-wrap');
                const tlEl   = document.querySelector('.timeline-wrap');
                if (listEl) listEl.scrollTop = 0;
                if (tlEl)   tlEl.scrollTop = 0;
            });
        };

        /* 点击徽章跳转分类（卡片 / 时间线 / 阅读视图） */
        const jumpToCategory = codeStr => {
            const parts = String(codeStr || '')
                .trim()
                .split(/\s+/)
                .filter(Boolean);

            const l1 = parts[0] || null;
            const l2 = parts[1] || null;

            if (!l1 || !L1_CODES.has(l1)) return;

            // 复用统一逻辑：设筛选 + 若在阅读视图则退回列表
            selectCategory(l1, l2);
        };

        const doSearch = () => {
            if (keyword.value.trim()) {
                const map = {};
                for (const g of groupedDocs.value) map[g.code] = true;
                expandedGroups.value = map;
            }
        };
        watch(keyword, () => { if (keyword.value.trim()) doSearch(); });

        /* ---------- URL ---------- */
        const pushUrlForDoc = (id, replace = false) => {
            const url = buildDocUrl(id);
            const cur = window.location.pathname + window.location.search;
            if (cur === url) return;
            replace ? history.replaceState({ id }, '', url)
                    : history.pushState({ id }, '', url);
        };
        const pushUrlForList = (replace = false) => {
            const url = buildListUrl();
            const cur = window.location.pathname + window.location.search;
            if (cur === url) return;
            replace ? history.replaceState({}, '', url)
                    : history.pushState({}, '', url);
        };

        /* ---------- 加载 ---------- */
        const loadAll = async () => {
            loadingDocs.value = true;
            try {
                const res = await fetch(api('/docapi/documents'));
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const data = await res.json();
                documents.value = data.docs || data.documents || [];
            } catch (e) {
                console.error('加载失败:', e);
                ElementPlus.ElMessage.error('文档列表加载失败');
            } finally {
                loadingDocs.value = false;
            }
        };

        const loadStats = async () => {
            statsLoading.value = true;
            try {
                const res = await fetch(api('/docapi/stats'));
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                stats.value = await res.json();
            } catch (e) {
                console.error('统计加载失败:', e);
                stats.value = null;
            } finally {
                statsLoading.value = false;
            }
        };

        const openStats = async () => {
            statsDialog.value = true;
            if (!stats.value) await loadStats();
        };

        const openFromStats = async id => {
            statsDialog.value = false;
            await openDoc({ id });
        };

        /* ---------- 打开文档 ---------- */
        const stripFrontmatter = c =>
            String(c || '').replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, '');

        let contentRequest = null;
        const openDoc = async (docOrId, forceReload = false, updateUrl = true) => {
            let doc = docOrId;
            if (typeof doc === 'string' || typeof doc === 'number') {
                doc = { id: String(doc) };
            }
            if (!doc || !doc.id) return;

            if (!forceReload && currentDoc.value && currentDoc.value.id === doc.id) {
                view.value = 'read';
                if (updateUrl) pushUrlForDoc(doc.id);
                return;
            }

            if (contentRequest) contentRequest.abort();
            const controller = new AbortController();
            contentRequest = controller;
            view.value = 'read';
            currentDoc.value = doc;
            currentContent.value = '';
            renderedHtml.value = '';
            loadingContent.value = true;
            missingId.value = '';
            if (updateUrl) pushUrlForDoc(doc.id);

            try {
                const res = await fetch(
                    api(`/docapi/document/${encodeURIComponent(doc.id)}`),
                    { signal: controller.signal }
                );
                if (controller.signal.aborted || contentRequest !== controller) return;

                if (res.status === 404) {
                    missingId.value = doc.id;
                    currentDoc.value = null;
                    return;
                }
                if (!res.ok) throw new Error(`HTTP ${res.status}`);

                const data = await res.json();
                if (controller.signal.aborted || contentRequest !== controller) return;
                if (typeof data.content !== 'string') {
                    throw new Error('缺少 content 字段');
                }
                if (data.meta) {
                    currentDoc.value = { ...doc, ...data.meta };
                }

                const raw = stripFrontmatter(data.content);
                currentContent.value = raw;
                renderedHtml.value = markdownToHtml(raw);

                if (readerWrap.value) readerWrap.value.scrollTop = 0;
            } catch (e) {
                if (e.name === 'AbortError' || contentRequest !== controller) return;
                console.error('[reader] 失败:', e);
                ElementPlus.ElMessage.error(e.message || '内容加载失败');
                currentContent.value = '';
                renderedHtml.value = '';
            } finally {
                if (contentRequest === controller) {
                    loadingContent.value = false;
                    contentRequest = null;
                }
            }
        };

        const closeReader = (updateUrl = true) => {
            if (contentRequest) {
                contentRequest.abort();
                contentRequest = null;
            }
            loadingContent.value = false;
            view.value = 'list';
            currentDoc.value = null;
            currentContent.value = '';
            renderedHtml.value = '';
            missingId.value = '';
            if (updateUrl) pushUrlForList();
        };

        const copyLink = async () => {
            if (!currentDoc.value) return;
            const base = window.location.origin + window.location.pathname;
            const url = `${base}?doc=${encodeURIComponent(currentDoc.value.id)}`;
            try {
                if (navigator.clipboard && window.isSecureContext) {
                    await navigator.clipboard.writeText(url);
                } else {
                    const ta = document.createElement('textarea');
                    ta.value = url;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                }
                ElementPlus.ElMessage.success('链接已复制');
            } catch (e) {
                ElementPlus.ElMessage.warning('复制失败：' + url);
            }
        };

        const updateIsMobile = () => {
            isMobile.value = window.innerWidth <= 768;
            if (!isMobile.value) sidebarOpen.value = false;
        };
        window.addEventListener('resize', updateIsMobile);

        const handlePopState = async () => {
            const id = parseDocIdFromUrl();
            if (!id) { closeReader(false); return; }
            if (currentDoc.value && currentDoc.value.id === id &&
                view.value === 'read') return;
            const hit = documents.value.find(d => d.id === id);
            await openDoc(hit || { id }, true, false);
        };
        window.addEventListener('popstate', handlePopState);
        const handleEscape = e => {
            if (e.key === 'Escape' && sidebarOpen.value) sidebarOpen.value = false;
        };
        window.addEventListener('keydown', handleEscape);

        onMounted(async () => {
            updateIsMobile();
            await loadAll();
            loadStats();

            try {
                const savedMode = localStorage.getItem('reader.viewMode');
                if (savedMode === 'timeline' || savedMode === 'card') {
                    viewMode.value = savedMode;
                } else {
                    viewMode.value = 'timeline';
                }
            } catch (e) {
                viewMode.value = 'timeline';
            }

            const initialId = parseDocIdFromUrl();
            if (initialId) {
                history.replaceState({ id: initialId }, '',
                    buildDocUrl(initialId));
                const hit = documents.value.find(d => d.id === initialId);
                await openDoc(hit || { id: initialId }, true);
            } else if (window.location.search.includes('doc=')) {
                history.replaceState({}, '', buildListUrl());
            }
        });

        watch(viewMode, v => {
            try { localStorage.setItem('reader.viewMode', v); } catch (e) {}
        });

        onUnmounted(() => {
            window.removeEventListener('resize', updateIsMobile);
            window.removeEventListener('popstate', handlePopState);
            window.removeEventListener('keydown', handleEscape);
            if (contentRequest) contentRequest.abort();
        });

        return {
            documents, loadingDocs, keyword,
            activeCat, activeSub,
            expandedCats, expandedGroups,
            view, viewMode, sidebarOpen,
            timelineField, timelineOrder,
            datePresets, rangePreset, dateStart, dateEnd, selectedDayKeys,
            currentDoc, currentContent, renderedHtml,
            loadingContent, missingId,
            stats, statsLoading, statsDialog,
            isMobile,
            listWrap, readerWrap,
            L1_META,

            filteredDocs, groupedDocs, categoryTree,
            timelineGroups, timelineTotal, timelineSummary, timelineMissingDates,
            timelineChart, selectedSummary,
            totalDocs, overallAvg, readingTime, currentCat,
            statsMap,

            parseCategory, getCleanTitle, getPreview, getWordCount,
            badgeStyle,
            formatNum, formatExactNum, shortNum, trendHeight,
            formatDate, formatDateTime, formatTimeOfDay,
            highlight,

            toggleCat, toggleGroup, selectCategory, jumpToCategory,
            doSearch,
            applyDatePreset, markCustomRange, clearDateRange,
            toggleDaySelection, isDaySelected, selectVisibleDays, clearSelectedDays,
            loadAll, loadStats, openStats, openFromStats,
            openDoc, closeReader, copyLink,
        };
    }
});

for (const [key, comp] of Object.entries(typeof ElementPlusIconsVue !== 'undefined' ? ElementPlusIconsVue : {})) {
    app.component(key, comp);
}
app.use(ElementPlus);
app.mount('#app');
</script>


<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">文档笔记 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
(function(){
  "use strict";
  var BASE = window.API_BASE || location.pathname.replace(/\/[^/]*$/, "");
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "文档笔记 · 首次设置" : "文档笔记 · 登录";
    document.getElementById("authBtn").textContent = setup ? "创建账户并进入" : "登录";
    document.getElementById("authForm").dataset.mode = setup ? "setup" : "login";
  }
  fetch(BASE + "/api/session", {cache:"no-store"})
    .then(function(r){return r.json();})
    .then(function(s){ if(!s.authenticated) showGate(!!s.setup_required); })
    .catch(function(){ showGate(false); });
  document.getElementById("authForm").addEventListener("submit", function(e){
    e.preventDefault();
    var mode = e.target.dataset.mode || "login";
    fetch(BASE + "/api/" + mode, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        username: document.getElementById("authUser").value.trim(),
        password: document.getElementById("authPass").value
      })
    }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
      .then(function(res){
        if(res.ok){ location.reload(); }
        else{ document.getElementById("authErr").textContent = (res.d && res.d.message) || "登录失败，请重试"; }
      })
      .catch(function(){ document.getElementById("authErr").textContent = "网络错误，请重试"; });
  });
})();
</script>
</body>
</html>''',
}
