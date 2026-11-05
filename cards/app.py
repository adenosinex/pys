"""Cognitive cards （认知卡片）, ported from the user's myweb 4card
blueprint (media/4card). Source of truth stays his Markdown card
files (frontmatter + body) with cover images alongside, kept in the
platform data directory; the search index is rebuilt from the files
on startup and after every change, exactly like the original engine.
On top of the original surface (search / tags / tag frequencies /
stats / edit / upload / update_stats / covers) this port adds a full
backup export/import (cards + covers, de-duplicated by id). The
project carries no authentication of its own: the App Hub platform
gate decides who gets in."""
import base64
import json
import os
import re
import sqlite3  # noqa: F401  (kept for parity with the original stack)
import time
from collections import Counter
from urllib.parse import parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")


def _platform_data_dir(base):
    """Persistent data directory: the platform's DATA_DIR env var when
    provided, else the ./data symlink the panel creates inside the
    project folder (pointing at the central data directory), else
    ./.runtime for standalone local runs."""
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    link = os.path.join(base, "data")
    if os.path.isdir(link):
        return link
    return os.path.join(base, ".runtime")


DATA_DIR = _platform_data_dir(BASE)
CARDS_DIR = os.path.join(DATA_DIR, "cards_data")
COVERS_DIR = os.path.join(DATA_DIR, "covers")
os.makedirs(CARDS_DIR, exist_ok=True)
os.makedirs(COVERS_DIR, exist_ok=True)

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>认知索引层</title>
      <script src="static/cdn/tailwindcss3.4.17.js"></script>
    <script src="static/cdn/echarts.min.js"></script>
    <script>
        tailwind.config = { darkMode: 'class' }
    </script>
    <style>
        /* 卡片最小宽度，避免宽屏4列时标题、标签被操作按钮挤成竖排 */
        #cardGrid { grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr)); }
        .query-title { min-width: 0; overflow-wrap: anywhere; word-break: normal; }
        .tag-chip { max-width: 100%; overflow-wrap: anywhere; word-break: normal; }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 font-sans min-h-screen p-6">
    <div class="max-w-6xl mx-auto">
        <header class="mb-8 flex flex-col gap-5">
            <div class="flex gap-3">
                <input type="text" id="searchInput" placeholder="检索认知库..." 
                    class="flex-1 px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg focus:outline-none focus:border-green-500 transition text-lg">
                <button onclick="fetchCards()" class="px-6 py-3 bg-green-600 hover:bg-green-500 text-white rounded-lg transition font-medium text-lg shadow-lg shadow-green-900/20">搜索</button>
                <div id="searchCount" 
                     class="px-4 py-3 bg-gray-800 border border-gray-700 rounded-lg text-gray-400">
                </div>
                <button onclick="openModal('uploadModal', true)" class="px-6 py-3 bg-gray-700 hover:bg-gray-600 rounded-lg transition font-medium text-lg">录入新卡片</button>
                <button onclick="resetHome()" class="px-6 py-3 bg-gray-800 border border-gray-700 hover:bg-gray-700 rounded-lg transition text-lg">回到首页</button>
            </div>
            
            <div class="flex flex-col gap-2">
                <div class="flex justify-between items-end px-1">
                    <h2 class="text-gray-400 text-sm font-bold uppercase tracking-wider">全局认知标签</h2>
                    <div class="flex gap-3 items-center">
                        <div id="statsContainer" class="text-sm text-gray-400 bg-gray-800 px-3 py-1 rounded-full border border-gray-700">
                            载入统计中...
                        </div>
                        <button onclick="doExportCards()" class="text-sm text-blue-400 hover:text-blue-300 font-medium px-2 border-l border-gray-700">导出备份</button><button onclick="document.getElementById('cardsImportFile').click()" class="text-sm text-blue-400 hover:text-blue-300 font-medium px-2">导入备份</button><input type="file" id="cardsImportFile" accept=".json,application/json" style="display:none" onchange="doImportCards(this)">
                    </div>
                </div>
                <div id="tagContainer" class="flex flex-wrap gap-3 p-3 bg-gray-800/50 rounded-xl border border-gray-800 min-h-[60px] items-center"></div>
            </div>
        </header>

        <div id="keyResult" class="hidden mb-6 bg-gray-800 border border-gray-700 rounded-xl p-5"></div>
        <div id="cardGrid" class="grid gap-6"></div>
    </div>

    <div id="detailModal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 p-4 overflow-y-auto" onclick="closeOnBg(event, 'detailModal')">
        <div class="bg-gray-800 w-full max-w-4xl rounded-xl shadow-2xl flex flex-col my-auto transition-transform duration-200">
            <div class="p-5 border-b border-gray-700 flex justify-between items-center bg-gray-900/30">
                <h3 id="modalTitle" class="text-2xl font-bold text-white"></h3>
                <button onclick="closeModal('detailModal')" class="text-gray-400 hover:text-white transition text-2xl leading-none">&times;</button>
            </div>
            
            <div class="p-6 flex flex-col gap-6 max-h-[80vh] overflow-y-auto">
                <div id="modalTags" class="flex flex-wrap gap-2"></div>
                
                <div id="modalStatsDashboard" class="hidden flex-col gap-5 border border-gray-700 bg-gray-900/40 p-5 rounded-xl shadow-inner">
                    <h4 class="text-sm font-bold text-green-400 uppercase tracking-wider flex items-center gap-2">
                        <span>📊</span> 物理资源分析特征
                    </h4>
                    
                    <div id="statsSummaryGrid" class="grid grid-cols-2 md:grid-cols-4 gap-3"></div>
                    
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div class="bg-gray-800/80 border border-gray-700 rounded-lg p-2 h-56 relative">
                            <div id="chartSize" class="w-full h-full"></div>
                        </div>
                        <div class="bg-gray-800/80 border border-gray-700 rounded-lg p-2 h-56 relative">
                            <div id="chartDuration" class="w-full h-full"></div>
                        </div>
                        <div class="bg-gray-800/80 border border-gray-700 rounded-lg p-2 h-56 relative">
                            <div id="chartResolution" class="w-full h-full"></div>
                        </div>
                        <div class="bg-gray-800/80 border border-gray-700 rounded-lg p-2 h-56 relative">
                            <div id="chartTimeline" class="w-full h-full"></div>
                        </div>
                    </div>
                </div>

                <div class="whitespace-pre-wrap text-gray-300 leading-relaxed text-base pt-2 border-t border-gray-700/50" id="modalBody"></div>
            </div>
            
            <div class="p-4 border-t border-gray-700 flex justify-end gap-3 bg-gray-900/30">
                <button onclick="closeModal('detailModal')" class="px-6 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium">关闭</button>
            </div>
        </div>
    </div>

    <div id="editModal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 p-4" onclick="closeOnBg(event, 'editModal')">
        <div class="bg-gray-800 w-full max-w-md rounded-xl shadow-2xl flex flex-col transition-transform duration-200">
            <div class="p-5 border-b border-gray-700 bg-gray-900/30">
                <h3 class="text-xl font-bold">快速编辑</h3>
            </div>
            <div class="p-6 flex flex-col gap-4">
                <input type="hidden" id="editCardId">
                <div class="bg-gray-900/50 p-3 rounded-lg border border-gray-700/50 flex flex-col gap-4">
                    <div>
                        <label class="block text-base font-bold text-blue-400 mb-2">🔑 检索 Key (主标题)</label>
                        <input type="text" id="editQueryInput" placeholder="用空格分割多个key代表同一个对象" class="w-full px-4 py-3 text-lg font-medium text-white bg-gray-900 border-2 border-gray-600 rounded-lg focus:outline-none focus:border-blue-500 transition shadow-inner placeholder-gray-500">
                    </div>
                    <div>
                        <label class="block text-base font-bold text-green-400 mb-2">🏷️ 重新分配标签 (任意分隔符)</label>
                        <input type="text" id="editTagsInput" class="w-full px-4 py-3 text-lg font-medium text-white bg-gray-900 border-2 border-gray-600 rounded-lg focus:outline-none focus:border-green-500 transition shadow-inner placeholder-gray-600">
                    </div>
                </div>
            </div>
            <div class="p-4 border-t border-gray-700 flex justify-end gap-3 bg-gray-900/30">
                <button onclick="closeModal('editModal')" class="px-5 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">取消</button>
                <button onclick="submitEdit()" class="px-6 py-2 bg-green-600 hover:bg-green-500 text-white rounded-lg font-bold shadow-lg shadow-green-900/20 transition">保存修改</button>
            </div>
        </div>
    </div>

  <div id="uploadModal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 p-4" onclick="closeOnBg(event, 'uploadModal')">
        <div class="bg-gray-800 w-full max-w-lg rounded-xl shadow-2xl flex flex-col transition-transform duration-200 max-h-[90vh]">
            
            <div class="p-5 border-b border-gray-700 bg-gray-900/30 shrink-0">
                <h3 class="text-xl font-bold">录入新认知</h3>
            </div>
            
            <div class="p-6 flex flex-col gap-5 overflow-y-auto">
                <div id="pasteArea" class="border-2 border-dashed border-gray-600 rounded-lg p-4 text-center text-gray-400 relative hover:border-green-500 transition bg-gray-900/50 shrink-0">
                    <input type="file" id="upCover" accept="image/*" class="absolute inset-0 w-full h-full opacity-0 cursor-pointer" onchange="previewImage(this.files[0])">
                    <div id="uploadHint" class="pointer-events-none py-2">点击选择文件，或直接此处 Ctrl+V 粘贴图片</div>
                    <img id="previewImg" class="hidden max-h-48 mx-auto rounded mt-2 object-contain pointer-events-none shadow-md">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">检索 Key (主标题)</label>
                    <input type="text" id="upQuery" class="w-full px-4 py-2.5 text-base bg-gray-900 border border-gray-700 rounded-lg focus:outline-none focus:border-green-500 transition">
                </div>
                <div class="bg-gray-900/50 p-3 rounded-lg border border-gray-700/50">
                    <label class="block text-base font-bold text-green-400 mb-2">🏷️ 灵魂标签</label>
                    <input type="text" id="upTags" class="w-full px-4 py-3 text-lg font-medium text-white bg-gray-900 border-2 border-gray-600 rounded-lg focus:outline-none focus:border-green-500 transition shadow-inner">
                </div>
                <div>
                    <label class="block text-sm font-medium text-gray-300 mb-1">描述补充 (Markdown)</label>
                    <textarea id="upDesc" rows="3" class="w-full px-4 py-2 text-sm bg-gray-900 border border-gray-700 rounded-lg focus:outline-none focus:border-green-500 resize-y transition"></textarea>
                </div>
            </div>
            
            <div class="p-4 border-t border-gray-700 flex justify-end gap-3 bg-gray-900/30 shrink-0">
                <button onclick="closeModal('uploadModal')" class="px-5 py-2.5 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">取消</button>
                <button onclick="submitUpload()" class="px-6 py-2.5 bg-green-600 hover:bg-green-500 text-white rounded-lg font-bold shadow-lg shadow-green-900/20 transition">确认保存</button>
            </div>
        </div>
    </div>

    <script> 
    // ================= 甜点级极速图片压缩引擎 =================
        function compressImage(file) {
            return new Promise((resolve, reject) => {
                // 【优化 1：极速放行】如果文件本身体积很小（<250KB），且不需要转格式，直接放行，0 延迟
                if (file.size < 250 * 1024 && (file.type === 'image/jpeg' || file.type === 'image/webp')) {
                    return resolve(file);
                }

                const reader = new FileReader();
                reader.readAsDataURL(file);
                reader.onload = (event) => {
                    const img = new Image();
                    img.src = event.target.result;
                    img.onload = () => {
                        // 【优化 2：放宽分辨率】使用 1280px 作为长边上限，保证高分屏下的清晰度
                        const maxEdge = 1280;
                        let width = img.width;
                        let height = img.height;

                        // 等比例缩放逻辑（处理横图和竖图）
                        if (width > maxEdge || height > maxEdge) {
                            if (width > height) {
                                height = Math.round((height * maxEdge) / width);
                                width = maxEdge;
                            } else {
                                width = Math.round((width * maxEdge) / height);
                                height = maxEdge;
                            }
                        }

                        const canvas = document.createElement('canvas');
                        canvas.width = width;
                        canvas.height = height;
                        const ctx = canvas.getContext('2d');

                        // 开启适度的平滑抗锯齿
                        ctx.imageSmoothingEnabled = true;
                        ctx.imageSmoothingQuality = 'medium';

                        // 填充纯白背景（抹除透明通道冗余，防止转 JPG 变黑）
                        ctx.fillStyle = '#FFFFFF';
                        ctx.fillRect(0, 0, width, height);
                        ctx.drawImage(img, 0, 0, width, height);

                        // 【优化 3：甜点压缩率】输出 0.85 画质的 JPEG，视觉无损且完美压制在 100~250KB 左右
                        canvas.toBlob((blob) => {
                            // 为了避免没有后缀名的剪贴板文件，统一强制命名为 .jpg
                            const originalName = file.name || 'clipboard_image';
                            const newFileName = originalName.replace(/\.[^/.]+$/, "") + "_opt.jpg";
                            
                            const compressedFile = new File([blob], newFileName, {
                                type: 'image/jpeg',
                                lastModified: Date.now(),
                            });
                            resolve(compressedFile);
                        }, 'image/jpeg', 0.85);
                    };
                    img.onerror = error => reject(error);
                };
                reader.onerror = error => reject(error);
            });
        }

    const API_BASE = location.pathname.replace(/\/[^/]*$/, "") + "/";
    const COLLECT_API_BASE = 'http://192.168.31.240'; // 文件检索服务地址
    const COLLECT_MAX_GB = null; // null 使用后端默认限制；可指定数值如 20 
        let currentTags = new Set();
        let echartInstances = []; 
        
        // --- 懒加载核心状态变量 ---
        let allCardsData = [];          // 存储从后端拿到的所有数据
        let currentRenderIndex = 0;     // 当前已渲染的数量
        const RENDER_CHUNK_SIZE = 12;   // 每次滚动加载的卡片数
        let observer = null;            // 滚动监听器

        window.addEventListener('resize', () => {
            echartInstances.forEach(chart => chart && chart.resize());
        });

        document.addEventListener('DOMContentLoaded', () => {
            fetchStats();
            fetchTags(); 
            fetchCards();
            document.getElementById('searchInput').addEventListener('keypress', e => { if (e.key === 'Enter') fetchCards(); });
            document.addEventListener('paste', handlePaste);
            
            // 初始化滚动监听器：距离底部 300px 时提前触发加载
            observer = new IntersectionObserver((entries) => {
                if (entries[0].isIntersecting) {
                    observer.disconnect();
                    renderNextChunk();
                }
            }, { rootMargin: '300px' });
        });

        // 统计与标签加载逻辑保持不变
        async function fetchStats() {
            try {
                const res = await fetch(`${API_BASE}api/stats`);
                const data = await res.json();
                document.getElementById('statsContainer').innerHTML = `
                    总计入库 <span class="text-green-400 font-bold">${data.total_cards}</span> 项 / 
                    共有 <span class="text-green-400 font-bold">${data.total_tags}</span> 个灵魂标签
                `;
            } catch (err) { console.error("加载统计失败:", err); }
        }

        function handlePaste(e) {
            const uploadModal = document.getElementById('uploadModal');
            if (uploadModal.classList.contains('hidden')) return; 
            const items = (e.clipboardData || e.originalEvent.clipboardData).items;
            for (let item of items) {
                if (item.type.indexOf("image") !== -1) {
                    const blob = item.getAsFile();
                    previewImage(blob);
                    const dataTransfer = new DataTransfer();
                    dataTransfer.items.add(new File([blob], "pasted_image.png", { type: blob.type }));
                    document.getElementById('upCover').files = dataTransfer.files;
                    break;
                }
            }
        }

        function previewImage(file) {
            if (!file) return;
            const img = document.getElementById('previewImg');
            const hint = document.getElementById('uploadHint');
            img.src = URL.createObjectURL(file);
            img.classList.remove('hidden');
            hint.classList.add('hidden');
        }

async function fetchTags() {
            try {
                const res = await fetch(`${API_BASE}api/tags_freq`);
                const data = await res.json();
                const tagsList = data.tag_frequencies || []; 
                
                const container = document.getElementById('tagContainer');
                container.innerHTML = tagsList.map(item => `
                    <button onclick="toggleTag('${item.tag}')" id="tag-${item.tag}" 
                        class="px-4 py-2 text-sm md:text-base font-medium rounded-full border border-gray-600 bg-gray-800 text-gray-300 hover:bg-gray-700 hover:text-white transition cursor-pointer shadow-sm flex items-center gap-1.5">
                        <span>${item.tag}</span>
                        <span class="count-badge text-xs bg-gray-700 text-gray-400 px-1.5 py-0.5 rounded-full font-sans">${item.count}</span>
                    </button>
                `).join('');
                
                currentTags.forEach(t => updateTagStyle(t, true));
            } catch (err) { console.error(err); }
        }

        function toggleTag(tag) {
            currentTags.has(tag) ? currentTags.delete(tag) : currentTags.add(tag);
            updateTagStyle(tag, currentTags.has(tag));
            fetchCards();
        }

        function updateTagStyle(tag, isActive) {
            const el = document.getElementById(`tag-${tag}`);
            if(!el) return;
            
            const badge = el.querySelector('.count-badge');
            
            if (isActive) {
                el.className = "px-4 py-2 text-sm md:text-base font-bold rounded-full border-2 border-green-500 bg-green-500/20 text-green-400 transition cursor-pointer shadow-md shadow-green-900/30 scale-105 transform flex items-center gap-1.5";
                if (badge) badge.className = "count-badge text-xs bg-green-500/30 text-green-300 px-1.5 py-0.5 rounded-full font-sans";
            } else {
                el.className = "px-4 py-2 text-sm md:text-base font-medium rounded-full border border-gray-600 bg-gray-800 text-gray-300 hover:bg-gray-700 hover:text-white transition cursor-pointer shadow-sm flex items-center gap-1.5";
                if (badge) badge.className = "count-badge text-xs bg-gray-700 text-gray-400 px-1.5 py-0.5 rounded-full font-sans";
            }
        }
        function resetHome() {
            document.getElementById('searchInput').value = '';
            currentTags.clear();
            fetchTags(); 
            fetchCards();
        }

        function escapeHTML(str) {
            if (typeof str !== 'string') return str;
            return str.replace(/[&<>'"]/g, tag => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            }[tag]));
        }

        /**
         * 跳转到外部搜索页面
         * 若 key 包含空格分隔的多个关键词，默认只跳转第一个
         */
        function jumpSearch(key) {
            if (!key) return;
            const keys = String(key).split(/\s+/).filter(Boolean);
            if (keys.length === 0) return;
            const first = keys[0]; // 默认跳转第一个空格分隔的关键词
            window.open(`http://192.168.31.240/?search=${encodeURIComponent(first)}`, '_blank');
        }

        /**
         * 查询 key 候选数量，在 h4 内部末尾追加显示；数量为 0 时不显示
         * 支持空格分隔的多关键词：
         *  - 总数 = 各关键词候选数量之和
         *  - 多关键词时，候选数量后追加每个关键词明细，每个明细可点击跳转对应关键词
         * @param {string} key
         * @param {HTMLElement} parentEl 父元素（h4）
         */
        async function countByKey(key, parentEl) {
            if (!key || !parentEl) return;

            // 1. 清掉上一次的结果（重复点击/重复初始化都不会堆积）
            parentEl.querySelectorAll('.key-count-result').forEach(el => el.remove());

            // 2. 空格分隔拆分为多个关键词
            const keys = String(key).split(/\s+/).filter(Boolean);
            if (keys.length === 0) return;

            try {
                // 3. 多次并发请求（每个关键词一个请求）
                const results = await Promise.all(keys.map(k =>
                    fetch(`http://192.168.31.240/videos/count?key=${encodeURIComponent(k)}`)
                        .then(r => r.json())
                        .then(d => Number(d.total) || 0)
                        .catch(() => 0)
                ));

                // 4. 整体数量之和
                const total = results.reduce((sum, n) => sum + n, 0);

                // 5. 数量为 0 直接不显示
                if (total <= 0) return;

                // 6. 有数量时才追加结果节点
                const box = document.createElement('div');
                box.className = 'key-count-result text-sm font-normal mt-1';

                // 6.1 总数
                const totalSpan = document.createElement('span');
                totalSpan.className = 'text-green-400';
                totalSpan.textContent = `候选数量：${total}`;
                box.appendChild(totalSpan);

                // 6.2 多关键词时，在总数后追加每个关键词的明细（可点击跳转）
                if (keys.length > 1) {
                    const detailWrap = document.createElement('span');
                    detailWrap.className = 'text-green-400';
                    detailWrap.appendChild(document.createTextNode('（'));

                    keys.forEach((k, i) => {
                        if (i > 0) {
                            const sep = document.createElement('span');
                            sep.className = 'text-gray-500 mx-1';
                            sep.textContent = '|';
                            detailWrap.appendChild(sep);
                        }
                        const link = document.createElement('span');
                        link.className = 'hover:text-green-300 underline cursor-pointer';
                        link.textContent = `${k}: ${results[i]}`;
                        // 点击明细跳转对应关键词，阻止冒泡避免重新触发 h4 的查询
                        link.addEventListener('click', (e) => {
                            e.stopPropagation();
                            jumpSearch(k);
                        });
                        detailWrap.appendChild(link);
                    });

                    detailWrap.appendChild(document.createTextNode('）'));
                    box.appendChild(detailWrap);
                }

                parentEl.appendChild(box);
            } catch (e) {
                console.error('countByKey 查询失败:', e);
            }
        }

        /**
         * 遍历指定范围内所有 h4.query-title，触发一次数量查询
         */
        function initQueryCounts(root = document) {
            root.querySelectorAll('h4.query-title[data-query]').forEach(h4 => {
                countByKey(h4.dataset.query, h4);
            });
        }

        // --- 核心：分步渲染 ---
        async function fetchCards() {
            try {
                const q = document.getElementById('searchInput').value;
                const tags = Array.from(currentTags).join(',');
                const res = await fetch(`${API_BASE}api/search?q=${encodeURIComponent(q)}&tags=${encodeURIComponent(tags)}`);
                const cards = await res.json();
                if (!Array.isArray(cards)) return;

                // 重置懒加载状态
                allCardsData = cards;
                currentRenderIndex = 0;
                document.getElementById('cardGrid').innerHTML = ''; // 清空画板
                
                // 渲染第一批
                renderNextChunk();
            } catch (err) { console.error(err); }
        }

        function renderNextChunk() {
            if (currentRenderIndex >= allCardsData.length) return; // 已经到底了

            const chunk = allCardsData.slice(currentRenderIndex, currentRenderIndex + RENDER_CHUNK_SIZE);
            const grid = document.getElementById('cardGrid');

            const html = chunk.map(card => {
                let coverUrl = card.cover ? card.cover.replace(/^\.\/covers\//, '') : '';
                if(coverUrl && !coverUrl.startsWith('http')) coverUrl = `${API_BASE}covers/${coverUrl}`;
                let tagsStr = card.tags.join(' ').replace(/'/g, "\\'");
                let queryStr = card.query ? card.query.replace(/'/g, "\\'") : '';
                const safeCardInfo = escapeHTML(JSON.stringify(card));
                
                return `
                <div onclick='openDetail(${safeCardInfo})' 
                     class="bg-gray-800 border border-gray-700 rounded-xl overflow-hidden cursor-pointer hover:-translate-y-1.5 hover:border-gray-500 transition-all duration-200 shadow-lg flex flex-col group relative">
                    <div class="h-52 overflow-hidden bg-gray-900/80 relative flex justify-center items-center p-1">
                        <img src="${coverUrl}" 
     loading="lazy"
     onclick="event.stopPropagation(); jumpSearch('${queryStr}')"
     class="w-full h-full object-contain group-hover:scale-[1.02] transition duration-300 cursor-pointer"
     onerror="this.style.display='none'">
                    </div>
                    <div class="p-5 flex-1 flex flex-col">
                        <div class="flex items-start gap-2 mb-3 min-w-0">
                            <h4 data-query="${escapeHTML(card.query)}"
                                onclick="event.stopPropagation(); countByKey(this.dataset.query, this)"
                                class="query-title flex-1 min-w-0 font-bold text-lg text-gray-100 leading-tight cursor-pointer hover:text-green-400">
                                ${escapeHTML(card.query)}
                            </h4>
                            ${card.stats_data ? '<span class="shrink-0 text-[10px] bg-green-900/50 text-green-400 border border-green-700 px-1.5 py-0.5 rounded whitespace-nowrap">已校验</span>' : ''}
                        </div>
                        <div class="flex flex-col gap-3 mt-auto pt-3 border-t border-gray-700/50 relative min-w-0">
                            <div class="flex flex-wrap gap-2 w-full min-w-0">
                                ${card.tags.map(t => `<span onclick="event.stopPropagation(); toggleTag('${t}')" class="tag-chip bg-gray-900 border border-gray-600 hover:border-green-500 hover:text-green-400 text-gray-300 px-2.5 py-1 rounded text-xs font-medium z-10 relative transition shadow-sm">${escapeHTML(t)}</span>`).join('')}
                            </div>
                            <div class="flex flex-wrap justify-end gap-2 w-full z-10">
                                <button data-query="${escapeHTML(card.query || '')}" onclick="event.stopPropagation(); collectByKey(this.dataset.query, this)" class="px-2.5 py-1.5 bg-amber-600 hover:bg-amber-500 text-white text-xs font-bold rounded shadow-md whitespace-nowrap">收集</button>
                                <button onclick="event.stopPropagation(); openEditModal('${card.id}', '${queryStr}', '${tagsStr}')" class="px-2.5 py-1.5 bg-gray-600 hover:bg-gray-500 text-white text-xs font-bold rounded shadow-md whitespace-nowrap">编辑</button>
                                <button onclick="event.stopPropagation(); openUploadWithTags('${tagsStr}')" class="px-2.5 py-1.5 bg-blue-600 hover:bg-blue-500 text-white text-xs font-bold rounded shadow-md whitespace-nowrap">+ 同类</button>
                            </div>
                        </div>
                    </div>
                </div>`
            }).join('');

            // 追加渲染到 DOM，而不是覆盖
            grid.insertAdjacentHTML('beforeend', html);
            currentRenderIndex += RENDER_CHUNK_SIZE;

            // 默认加载：对本批新渲染的卡片自动触发数量查询
            const newCardEls = Array.from(grid.children).slice(-chunk.length);
            newCardEls.forEach(cardEl => {
                const h4 = cardEl.querySelector('h4.query-title[data-query]');
                if (h4) countByKey(h4.dataset.query, h4);
            });

            // 如果还有没渲染完的数据，把监听器挂在当前最后一张卡片上
            if (currentRenderIndex < allCardsData.length) {
                const cardsElements = grid.children;
                const lastCard = cardsElements[cardsElements.length - 1];
                if (lastCard) observer.observe(lastCard);
            }
        }

        // ================= 收集：预览后硬链接 =================
        async function collectByKey(query, button) {
            // 与原有跳转搜索一致：卡片多个 key 用空格分隔时取第一个。
            const key = String(query || '').trim().split(/\s+/)[0];
            if (!key || button.disabled) return;
            const originalText = button.textContent;
            button.disabled = true;
            button.textContent = '查询中...';
            try {
                const url = `${COLLECT_API_BASE}/videos/collect/preview?key=${encodeURIComponent(key)}`;
                const previewRes = await fetch(url);
                const preview = await previewRes.json().catch(() => ({}));
                if (!previewRes.ok) throw new Error(preview.error || preview.detail || `预览接口错误 HTTP ${previewRes.status}，请检查 Flask 后端是否已重启并注册路由`);
                if (!preview.count) {
                    alert(`关键词「${key}」没有匹配的文件`);
                    return;
                }

                const gb = n => (n / (1024 ** 3)).toFixed(2);
                const listed = preview.files.slice(0, 12).map(f =>
                    `${f.name} (${gb(f.size_bytes)} GiB)${f.exists === false ? ' [源文件不可用]' : ''}`
                ).join('\n');
                const rest = preview.count > 12 ? `\n...另有 ${preview.count - 12} 个文件` : '';
                const limit = COLLECT_MAX_GB !== null ? COLLECT_MAX_GB : preview.default_max_total_gb;
                const prompt = `收集关键词：${key}\n匹配：${preview.count} 个文件\n总体积：${gb(preview.total_size_bytes)} GiB\n大小限制：${limit ? `${limit} GiB` : '不限制'}\n\n${listed}${rest}\n\n确认创建硬链接吗？`;
                if (!confirm(prompt)) return;

                button.textContent = '收集中...';
                const linkRes = await fetch(`${COLLECT_API_BASE}/videos/collect`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key, max_total_gb: COLLECT_MAX_GB })
                });
                const result = await linkRes.json().catch(() => ({}));
                if (!linkRes.ok) throw new Error(result.error || result.detail || `创建硬链接失败 HTTP ${linkRes.status}`);

                const errorPreview = (result.failed || []).slice(0, 3)
                    .map(f => `${f.name}: ${f.reason}`).join('\n');
                alert(`收集完成：${key}\n成功：${result.linked_count} 个（${gb(result.linked_bytes)} GiB）\n跳过：${result.skipped_count} 个\n失败：${result.failed_count} 个\n目录：${result.target_dir}${errorPreview ? `\n\n失败示例：\n${errorPreview}` : ''}`);
            } catch (error) {
                alert(`收集失败：${error.message || error}`);
                console.error('collectByKey:', error);
            } finally {
                button.disabled = false;
                button.textContent = originalText;
            }
        }

        // ================= 核心：详情渲染与图表绘制 =================
        function openDetail(card) {
            document.getElementById('modalTitle').textContent = card.query;
            document.getElementById('modalBody').textContent = card.content_body || '暂无文字描述...';
            
            const modalTags = document.getElementById('modalTags');
            if (card.tags && card.tags.length > 0) {
                modalTags.innerHTML = card.tags.map(t => `<span class="bg-gray-700 border border-gray-500 text-gray-200 px-3 py-1 rounded-full text-sm font-medium shadow-sm">${escapeHTML(t)}</span>`).join('');
                modalTags.style.display = 'flex';
            } else {
                modalTags.style.display = 'none';
            }

            const dbContainer = document.getElementById('modalStatsDashboard');
            echartInstances.forEach(c => c && c.dispose());
            echartInstances = [];

            if (card.stats_data) {
                dbContainer.classList.remove('hidden');
                dbContainer.classList.add('flex');
                
                const s = card.stats_data.summary;
                const c = card.stats_data.charts;

                document.getElementById('statsSummaryGrid').innerHTML = `
                    <div class="bg-gray-800 p-3 rounded text-center border border-gray-700"><div class="text-xs text-gray-400 mb-1">物理文件数</div><div class="text-lg font-bold text-white">${s.file_count} <span class="text-xs text-gray-500 font-normal">个</span></div></div>
                    <div class="bg-gray-800 p-3 rounded text-center border border-gray-700"><div class="text-xs text-gray-400 mb-1">总体积规模</div><div class="text-lg font-bold text-blue-400">${s.total_size_gb} <span class="text-xs text-blue-500 font-normal">GB</span></div></div>
                    <div class="bg-gray-800 p-3 rounded text-center border border-gray-700"><div class="text-xs text-gray-400 mb-1">总计时长</div><div class="text-lg font-bold text-yellow-400">${s.total_duration_h} <span class="text-xs text-yellow-500 font-normal">H</span></div></div>
                    <div class="bg-gray-800 p-3 rounded text-center border border-gray-700"><div class="text-xs text-gray-400 mb-1">推导特征</div><div class="text-sm mt-1.5 font-bold text-green-400 leading-tight">${s.size_feature}</div></div>
                `;

                const darkTheme = 'dark';
                const commonOpt = { backgroundColor: 'transparent', tooltip: { trigger: 'item' } };

                const chartSize = echarts.init(document.getElementById('chartSize'), darkTheme);
                chartSize.setOption({
                    ...commonOpt,
                    title: { text: '容量分布', textStyle: { fontSize: 12, color: '#9ca3af' }, top: 0, left: 'center' },
                    grid: { top: 30, bottom: 20, left: 30, right: 10 },
                    xAxis: { type: 'category', data: Object.keys(c.size_dist), axisLabel: { color: '#6b7280' } },
                    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#374151' } } },
                    series: [{ data: Object.values(c.size_dist), type: 'bar', itemStyle: { borderRadius: [4, 4, 0, 0], color: '#3b82f6' } }]
                });

                const chartDur = echarts.init(document.getElementById('chartDuration'), darkTheme);
                chartDur.setOption({
                    ...commonOpt,
                    title: { text: '时长分布', textStyle: { fontSize: 12, color: '#9ca3af' }, top: 0, left: 'center' },
                    grid: { top: 30, bottom: 20, left: 30, right: 10 },
                    xAxis: { type: 'category', data: Object.keys(c.duration_dist), axisLabel: { color: '#6b7280' } },
                    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#374151' } } },
                    series: [{ data: Object.values(c.duration_dist), type: 'bar', itemStyle: { borderRadius: [4, 4, 0, 0], color: '#eab308' } }]
                });

                const resData = Object.entries(c.resolution_dist).map(([name, value]) => ({ name, value }));
                const chartRes = echarts.init(document.getElementById('chartResolution'), darkTheme);
                chartRes.setOption({
                    ...commonOpt,
                    title: { text: '画质分布', textStyle: { fontSize: 12, color: '#9ca3af' }, top: 0, left: 'center' },
                    series: [{ type: 'pie', radius: ['40%', '70%'], center: ['50%', '55%'], data: resData, label: { color: '#d1d5db' }, itemStyle: { borderRadius: 4, borderColor: '#1f2937', borderWidth: 2 } }]
                });

                const chartTime = echarts.init(document.getElementById('chartTimeline'), darkTheme);
                chartTime.setOption({
                    ...commonOpt,
                    tooltip: { trigger: 'axis' },
                    title: { text: '日期', textStyle: { fontSize: 12, color: '#9ca3af' }, top: 0, left: 'center' },
                    grid: { top: 30, bottom: 20, left: 30, right: 10 },
                    xAxis: { type: 'category', boundaryGap: false, data: Object.keys(c.timeline), axisLabel: { color: '#6b7280' } },
                    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#374151' } } },
                    series: [{ data: Object.values(c.timeline), type: 'line', smooth: true, areaStyle: { opacity: 0.3, color: '#22c55e' }, itemStyle: { color: '#22c55e' } }]
                });

                echartInstances.push(chartSize, chartDur, chartRes, chartTime);
            } else {
                dbContainer.classList.add('hidden');
                dbContainer.classList.remove('flex');
            }

            document.getElementById('detailModal').classList.replace('hidden', 'flex');
            
            if (card.stats_data) {
                setTimeout(() => echartInstances.forEach(c => c.resize()), 100);
            }
        }

        // ================= 模态框通用操作 =================
        function openModal(id, isReset = false) {
            if (id === 'uploadModal' && isReset) {
                document.getElementById('upCover').value = '';
                document.getElementById('previewImg').src = '';
                document.getElementById('previewImg').classList.add('hidden');
                document.getElementById('uploadHint').classList.remove('hidden');
                document.getElementById('upQuery').value = '';
                document.getElementById('upTags').value = '';
                document.getElementById('upDesc').value = '';
            }
            document.getElementById(id).classList.replace('hidden', 'flex');
            if (id === 'uploadModal') setTimeout(() => document.getElementById('upQuery').focus(), 100);
            if (id === 'editModal') setTimeout(() => document.getElementById('editQueryInput').focus(), 100);
        }

        function openUploadWithTags(tagsStr) {
            openModal('uploadModal', true); 
            document.getElementById('upTags').value = tagsStr; 
        }

        function openEditModal(cardId, queryStr, tagsStr) {
            document.getElementById('editCardId').value = cardId;
            document.getElementById('editQueryInput').value = queryStr;
            document.getElementById('editTagsInput').value = tagsStr;
            openModal('editModal');
        }

        function closeModal(id) {
            document.getElementById(id).classList.replace('flex', 'hidden');
        }

        function closeOnBg(e, id) { 
            if (e.target.id === id) {
                if (id === 'uploadModal' || id === 'editModal') {
                    const modalContent = e.target.querySelector('.bg-gray-800');
                    modalContent.classList.add('scale-[1.02]');
                    setTimeout(() => modalContent.classList.remove('scale-[1.02]'), 150);
                    return;
                }
                closeModal(id);
            } 
        }

        async function submitEdit() {
            const cardId = document.getElementById('editCardId').value;
            const fd = new FormData();
            fd.append('query', document.getElementById('editQueryInput').value);
            fd.append('tags', document.getElementById('editTagsInput').value);

            try {
                const res = await fetch(`${API_BASE}api/edit/${cardId}`, { method: 'POST', body: fd });
                if ((await res.json()).status === 'success') {
                    closeModal('editModal'); fetchStats(); fetchTags(); fetchCards();
                } else alert('修改失败');
            } catch (e) { alert('网络异常'); }
        }

// ================= 重写：接入压缩流程的上传函数 =================
        async function submitUpload() {
            const files = document.getElementById('upCover').files;
            const query = document.getElementById('upQuery').value.trim();
            if (!files.length || !query) return alert("截图和Key为必填");

            const submitBtn = document.querySelector('#uploadModal button.bg-green-600');
            const originalBtnText = submitBtn.innerText;
            submitBtn.innerText = "压缩处理中...";
            submitBtn.disabled = true;
            submitBtn.classList.add('opacity-50', 'cursor-not-allowed');

            try {
                const compressedFile = await compressImage(files[0], 800, 0.8);

                const fd = new FormData();
                fd.append('cover', compressedFile);
                fd.append('query', query);
                fd.append('tags', document.getElementById('upTags').value);
                fd.append('description', document.getElementById('upDesc').value);

                submitBtn.innerText = "高速上传中...";

                const res = await fetch(`${API_BASE}api/upload`, { method: 'POST', body: fd });
                if ((await res.json()).status === 'success') {
                    closeModal('uploadModal'); 
                    fetchStats(); 
                    fetchTags(); 
                    fetchCards();
                } else {
                    alert('录入失败，请检查服务端日志');
                }
            } catch (e) { 
                alert('处理异常: ' + e.message); 
                console.error(e);
            } finally {
                submitBtn.innerText = originalBtnText;
                submitBtn.disabled = false;
                submitBtn.classList.remove('opacity-50', 'cursor-not-allowed');
            }
        }
        
        </script>

<script>
async function doExportCards() {
    const r = await fetch(API_BASE + 'api/export', {cache: 'no-store'});
    if (!r.ok) { alert('导出失败：HTTP ' + r.status); return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = 'cards-backup.json';
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 400);
}
async function doImportCards(input) {
    const f = input.files[0]; input.value = '';
    if (!f) return;
    try {
        const parsed = JSON.parse(await f.text());
        const items = Array.isArray(parsed) ? parsed : (parsed.items || []);
        if (!items.length) { alert('备份里没有卡片'); return; }
        let added = 0, skipped = 0;
        for (let i = 0; i < items.length; i += 10) {
            const batch = items.slice(i, i + 10);
            const r = await fetch(API_BASE + 'api/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({items: batch})});
            const d = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
            added += d.added || 0; skipped += d.skipped || 0;
        }
        alert('导入完成：新增 ' + added + '，跳过 ' + skipped);
        location.reload();
    } catch (e) { alert('导入失败：' + e.message); }
}
</script>
</body>
</html>"""


# ------------------------------------------------------------ http utils

def json_response(start_response, payload, code=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response("%d OK" % code if code == 200 else "%d Error" % code,
                   [("Content-Type", "application/json; charset=utf-8"),
                    ("Content-Length", str(len(body)))])
    return [body]


def read_body(environ, limit=134217728):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    length = max(0, min(length, limit))
    stream = environ.get("wsgi.input")
    if stream is None or length == 0:
        return b""
    return stream.read(length)


def read_json(environ):
    raw = read_body(environ)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def parse_multipart(environ):
    """Minimal multipart/form-data parser.
    Returns (fields, files): fields maps name -> str; files maps
    name -> (filename, bytes)."""
    ctype = environ.get("CONTENT_TYPE", "")
    m = re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return {}, {}
    boundary = m.group(1).strip().strip('"').encode("utf-8", "replace")
    body = read_body(environ)
    fields, files = {}, {}
    if not body:
        return fields, files
    delimiter = b"--" + boundary
    for part in body.split(delimiter):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"--"):
            part = part[:-2]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        head, sep, content = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head_text = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', head_text)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', head_text)
        if file_m is not None:
            files[name] = (file_m.group(1), content)
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


_MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
         ".svg": "image/svg+xml", ".json": "application/json"}


def serve_file(start_response, path, environ):
    if not os.path.isfile(path):
        return json_response(start_response, {"error": "not found"}, 404)
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as fh:
        body = fh.read()
    start_response("200 OK", [("Content-Type", _MIME.get(ext, "application/octet-stream")),
                              ("Content-Length", str(len(body)))])
    return [body]


def download_json(start_response, payload, filename):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    start_response("200 OK", [("Content-Type", "application/json; charset=utf-8"),
                              ("Content-Disposition", 'attachment; filename="%s"' % filename),
                              ("Content-Length", str(len(body)))])
    return [body]


# ------------------------------------------------------------ card engine

def _parse_card_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read().lstrip("\ufeff").strip()
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        meta = {}
        for line in parts[1].strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                key, val = line.split(":", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key == "tags" and val.startswith("[") and val.endswith("]"):
                    tag_str = val[1:-1]
                    meta[key] = [t.strip().strip('"').strip("'")
                                 for t in tag_str.split(",") if t.strip()]
                else:
                    meta[key] = val
        meta["content_body"] = parts[2].strip()
        return meta
    except Exception:
        return None


_INDEX = None


def _sync_cards():
    global _INDEX
    cards = []
    for filename in os.listdir(CARDS_DIR):
        if not filename.endswith(".md"):
            continue
        meta = _parse_card_file(os.path.join(CARDS_DIR, filename))
        if meta and "id" in meta:
            cards.append(meta)
    _INDEX = cards
    return cards


def _index():
    if _INDEX is None:
        return _sync_cards()
    return _INDEX


def _card_view(meta):
    try:
        stats = json.loads(meta["stats_data"]) if meta.get("stats_data") else None
    except Exception:
        stats = None
    tags = meta.get("tags")
    if not isinstance(tags, list):
        tags = []
    return {"id": meta.get("id"), "type": meta.get("type", "unknown"),
            "query": meta.get("query", ""), "tags": tags,
            "cover": meta.get("cover", ""), "stats_data": stats,
            "content_body": meta.get("content_body", "")}


def _serialize_card(meta):
    lines = ["---", "id: %s" % meta["id"], "type: %s" % meta.get("type", "unknown"),
             'query: "%s"' % (meta.get("query", "") or "")]
    lines.append("tags: " + json.dumps(meta.get("tags") or [], ensure_ascii=False))
    if meta.get("cover"):
        lines.append("cover: " + meta["cover"])
    if meta.get("stats_data"):
        sd = meta["stats_data"]
        if not isinstance(sd, str):
            sd = json.dumps(sd, ensure_ascii=False)
        lines.append("stats_data: '%s'" % sd)
    lines.append("---")
    return "\n".join(lines) + "\n\n" + (meta.get("content_body") or "") + "\n"


# ------------------------------------------------------------ handlers

def h_search(environ, start_response):
    qs = parse_qs(environ.get("QUERY_STRING", ""))
    q = (qs.get("q") or [""])[0]
    tags_param = (qs.get("tags") or [""])[0]
    tags = [t for t in tags_param.split(",") if t] if tags_param else []
    results = []
    for meta in _index():
        if q and q not in (meta.get("query") or ""):
            continue
        ctags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        if tags and not all(t in ctags for t in tags):
            continue
        results.append(_card_view(meta))
    results.sort(key=lambda c: c["id"] or "", reverse=True)
    return json_response(start_response, results)


def h_tags(environ, start_response):
    all_tags = set()
    for meta in _index():
        if isinstance(meta.get("tags"), list):
            all_tags.update(meta["tags"])
    return json_response(start_response, sorted(all_tags))


def h_tags_freq(environ, start_response):
    counter = Counter()
    total = 0
    for meta in _index():
        if isinstance(meta.get("tags"), list):
            counter.update(meta["tags"])
            total += len(meta["tags"])
    return json_response(start_response, {
        "tag_frequencies": [{"tag": k, "count": v} for k, v in counter.most_common()],
        "total_instances": total})


def h_stats(environ, start_response):
    tag_set = set()
    cards = _index()
    for meta in cards:
        if isinstance(meta.get("tags"), list):
            tag_set.update(meta["tags"])
    return json_response(start_response,
                         {"total_cards": len(cards), "total_tags": len(tag_set)})


def h_sync(environ, start_response):
    cards = _sync_cards()
    return json_response(start_response,
                         {"status": "success", "message": "索引已刷新",
                          "total_cards": len(cards)})


def _md_path(card_id):
    safe = "".join(c for c in str(card_id) if c.isalnum() or c in "_-.")
    return os.path.join(CARDS_DIR, safe + ".md"), safe


def h_edit(environ, start_response, card_id):
    md_path, _ = _md_path(card_id)
    if not os.path.exists(md_path):
        return json_response(start_response,
                             {"status": "error", "message": "卡片文件不存在"}, 404)
    fields, _files = parse_multipart(environ)
    query_str = (fields.get("query") or "").strip()
    tags_str = fields.get("tags") or ""
    tags_list = [t.strip() for t in re.split(r"[,\s;，；、]+", tags_str) if t.strip()]
    tags_fm = json.dumps(tags_list, ensure_ascii=False)
    with open(md_path, "r", encoding="utf-8") as fh:
        content = fh.read()
    parts = content.split("---", 2)
    if len(parts) < 3:
        return json_response(start_response,
                             {"status": "error", "message": "Markdown Frontmatter 格式损坏"}, 500)
    fm_lines = parts[1].strip().split("\n")
    new_lines = []
    has_query = False
    for line in fm_lines:
        if line.startswith("tags:"):
            new_lines.append("tags: " + tags_fm)
        elif line.startswith("query:") and query_str:
            new_lines.append('query: "%s"' % query_str)
            has_query = True
        else:
            new_lines.append(line)
    if not has_query and query_str:
        new_lines.append('query: "%s"' % query_str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("---\n" + "\n".join(new_lines) + "\n---" + parts[2])
    _sync_cards()
    return json_response(start_response, {"status": "success", "message": "内容修改成功"})


def h_upload(environ, start_response):
    fields, files = parse_multipart(environ)
    if "cover" not in files:
        return json_response(start_response,
                             {"status": "error", "message": "缺少封面图片"}, 400)
    filename, blob = files["cover"]
    if not filename:
        return json_response(start_response,
                             {"status": "error", "message": "未选择文件"}, 400)
    query = (fields.get("query") or "未命名卡片").strip()
    tags_str = fields.get("tags") or ""
    description = (fields.get("description") or "").strip()
    card_id = "c_%d" % int(time.time())
    md_path, card_id = _md_path(card_id)
    ext = os.path.splitext(filename)[1].lower() or ".jpg"
    cover_name = card_id + ext
    with open(os.path.join(COVERS_DIR, cover_name), "wb") as fh:
        fh.write(blob)
    tags_list = [t.strip() for t in re.split(r"[,\s;，；、]+", tags_str) if t.strip()]
    meta = {"id": card_id, "type": "user_upload", "query": query,
            "tags": tags_list, "cover": "./covers/" + cover_name,
            "content_body": description}
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(_serialize_card(meta))
    _sync_cards()
    return json_response(start_response, {"status": "success", "message": "卡片录入成功"})


def h_update_stats(environ, start_response, card_id):
    stats = read_json(environ)
    if not stats:
        return json_response(start_response,
                             {"status": "error", "message": "未收到统计数据"}, 400)
    md_path, _ = _md_path(card_id)
    if not os.path.exists(md_path):
        return json_response(start_response,
                             {"status": "error", "message": "卡片文件不存在"}, 404)
    try:
        with open(md_path, "r", encoding="utf-8") as fh:
            content = fh.read().replace("\r\n", "\n")
        parts = content.split("---", 2)
        if len(parts) < 3:
            return json_response(start_response,
                                 {"status": "error", "message": "Markdown Frontmatter 格式损坏"}, 500)
        fm_lines = [ln for ln in parts[1].strip().split("\n")
                    if ln.strip() and not ln.startswith("stats_data:")]
        fm_lines.append("stats_data: '%s'" % json.dumps(stats, ensure_ascii=False))
        body_text = parts[2]
        target = "# 数据统计\n"
        if target in body_text:
            start_idx = body_text.find(target)
            next_idx = body_text.find("\n# ", start_idx + len(target))
            body_text = (body_text[:start_idx] + body_text[next_idx:]
                         if next_idx != -1 else body_text[:start_idx])
        body_text = body_text.strip("\n")
        body_text = "\n\n" + body_text + "\n" if body_text else "\n"
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write("---\n" + "\n".join(fm_lines) + "\n---" + body_text)
        _sync_cards()
        return json_response(start_response,
                             {"status": "success", "message": "图表数据已安全注入卡片头部"})
    except Exception as exc:
        return json_response(start_response,
                             {"status": "error", "message": "服务器写入异常: %s" % exc}, 500)


def h_cover(environ, start_response, name):
    safe = os.path.basename(unquote(name))
    return serve_file(start_response, os.path.join(COVERS_DIR, safe), environ)


def h_export(environ, start_response):
    items = []
    for meta in _index():
        item = _card_view(meta)
        cover_name = os.path.basename(meta.get("cover") or "")
        item["stats_data_raw"] = meta.get("stats_data") or None
        item["cover_file"] = cover_name or None
        item["cover_base64"] = None
        if cover_name:
            path = os.path.join(COVERS_DIR, cover_name)
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    item["cover_base64"] = base64.b64encode(fh.read()).decode("ascii")
        items.append(item)
    return download_json(start_response,
                         {"format": "cards-backup", "version": 1, "items": items},
                         "cards-backup.json")


def h_import(environ, start_response):
    data = read_json(environ)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return json_response(start_response, {"ok": False, "error": "备份格式不对。"}, 400)
    existing = {m.get("id") for m in _index()}
    added = skipped = 0
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            skipped += 1
            continue
        cid = str(it["id"])
        if cid in existing:
            skipped += 1
            continue
        cover_name = it.get("cover_file") or os.path.basename(it.get("cover") or "")
        blob = it.get("cover_base64")
        if blob and cover_name:
            try:
                raw = base64.b64decode(blob)
            except Exception:
                raw = b""
            if raw:
                with open(os.path.join(COVERS_DIR, os.path.basename(cover_name)), "wb") as fh:
                    fh.write(raw)
        stats_raw = it.get("stats_data_raw")
        if stats_raw is None and it.get("stats_data") is not None:
            stats_raw = json.dumps(it["stats_data"], ensure_ascii=False)
        meta = {"id": cid, "type": it.get("type") or "user_upload",
                "query": it.get("query") or "", "tags": it.get("tags") or [],
                "cover": ("./covers/" + os.path.basename(cover_name)) if cover_name else "",
                "stats_data": stats_raw, "content_body": it.get("content_body") or ""}
        md_path, _ = _md_path(cid)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(_serialize_card(meta))
        existing.add(cid)
        added += 1
    _sync_cards()
    return json_response(start_response, {"ok": True, "added": added, "skipped": skipped})


# ------------------------------------------------------------ router

def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/") or "/"

    if path == "/" and method == "GET":
        body = PAGE.encode("utf-8")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"),
                                  ("Content-Length", str(len(body)))])
        return [body]
    if path == "/api/session" and method == "GET":
        return json_response(start_response, {"authenticated": True, "via": "platform"})
    if path.startswith("/static/") and method == "GET":
        rel = os.path.normpath(path[len("/static/"):])
        full = os.path.join(STATIC_DIR, rel)
        if full.startswith(STATIC_DIR):
            return serve_file(start_response, full, environ)
        return json_response(start_response, {"error": "not found"}, 404)
    if path == "/api/search" and method == "GET":
        return h_search(environ, start_response)
    if path == "/api/tags" and method == "GET":
        return h_tags(environ, start_response)
    if path == "/api/tags_freq" and method == "GET":
        return h_tags_freq(environ, start_response)
    if path == "/api/stats" and method == "GET":
        return h_stats(environ, start_response)
    if path == "/api/sync" and method == "POST":
        return h_sync(environ, start_response)
    if path == "/api/upload" and method == "POST":
        return h_upload(environ, start_response)
    if path == "/api/export" and method == "GET":
        return h_export(environ, start_response)
    if path == "/api/import" and method == "POST":
        return h_import(environ, start_response)
    m = re.fullmatch(r"/api/edit/([^/]+)", path)
    if m and method == "POST":
        return h_edit(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/api/update_stats/([^/]+)", path)
    if m and method == "POST":
        return h_update_stats(environ, start_response, unquote(m.group(1)))
    m = re.fullmatch(r"/covers/(.+)", path)
    if m and method == "GET":
        return h_cover(environ, start_response, m.group(1))
    return json_response(start_response, {"error": "not found"}, 404)


app = application
