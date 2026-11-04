"""Record pages from the user's myweb, adapted for the hub
(API base derived from the page URL, vendored statics made
relative, auth gate injected)."""

PAGES = {
    'daily': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>日常项目统计</title>
    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <script src="static/cdn/axios1.1.min.js"></script>
    <script src="static/cdn/tailwindcss3.4.17.js"></script>
    <script src="static/cdn/chart.min.3.9.1.js"></script>
    <style>
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
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
<body class="bg-gray-50 min-h-screen">
<div id="app" class="mx-auto w-full max-w-xl px-3 py-4 sm:px-6 sm:py-8">
    <div class="rounded-2xl bg-white p-4 shadow-sm sm:p-6">

        <!-- 顶部：标题 + 导入导出 -->
        <div class="mb-4 flex items-center justify-between gap-3">
            <h1 class="text-xl font-bold text-blue-700 sm:text-2xl">日常项目统计</h1>
            <div class="flex shrink-0 gap-1.5">
                <button @click="exportCSV"
                        class="rounded-lg bg-green-500 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-green-600 active:scale-95 sm:text-sm">
                    导出
                </button>
                <input type="file" ref="fileInput" @change="importCSV" class="hidden" accept=".csv">
                <button @click="$refs.fileInput.click()"
                        class="rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-medium text-white shadow-sm transition hover:bg-amber-600 active:scale-95 sm:text-sm">
                    导入
                </button>
            </div>
        </div>

        <!-- 项目切换（移动端横向滚动） -->
        <div class="no-scrollbar -mx-4 mb-4 flex gap-2 overflow-x-auto border-b border-gray-100 px-4 pb-3 sm:mx-0 sm:px-0">
            <button v-for="pp in allProjects" :key="pp" @click="project = pp"
                    :class="project === pp
                        ? 'bg-blue-600 text-white shadow-md shadow-blue-500/30'
                        : 'bg-gray-100 text-gray-600 hover:bg-gray-200'"
                    class="shrink-0 rounded-full px-4 py-1.5 text-sm font-medium transition-all duration-200 active:scale-95">
                ${ pp }
            </button>
        </div>

        <!-- 记录表单 -->
        <form @submit.prevent="addRecord" class="mb-5 rounded-2xl border border-gray-100 bg-gray-50 p-3 sm:p-4">
            <div class="mb-3 flex items-center gap-2">
                <span class="text-base font-bold sm:text-lg"
                      :class="project === 'ejec' ? 'text-blue-700' : 'text-green-700'">${ project }</span>
                <span class="text-xs text-gray-400">记录一条新数据</span>
            </div>

            <div class="mb-3 flex gap-2">
                <template v-if="project === 'ejec'">
                    <input type="number" v-model="count" min="1" inputmode="numeric"
                           class="w-20 rounded-xl border border-gray-200 bg-white px-3 py-2.5 text-center text-base font-bold text-blue-700 focus:border-blue-400 focus:outline-none focus:ring-2 focus:ring-blue-100"
                           placeholder="次数">
                </template>
                <input v-model="remark"
                       class="min-w-0 flex-1 rounded-xl border border-gray-200 bg-white px-3 py-2.5 text-base focus:border-blue-400 focus:outline-none focus:ring-2 focus:ring-blue-100"
                       placeholder="备注（可选）">
            </div>

            <!-- 快捷次数 -->
            <div v-if="project === 'ejec'" class="mb-3 flex gap-2">
                <button v-for="n in squatOptions" :key="n" type="button" @click="quickSquat(n)"
                        class="flex-1 rounded-xl bg-blue-100 py-2 text-base font-bold text-blue-700 transition hover:bg-blue-200 active:scale-95">
                    ${ n }
                </button>
            </div>

            <!-- 大号记录按钮 -->
            <button type="submit"
                    class="w-full rounded-xl py-3.5 text-lg font-bold text-white transition-all duration-150 active:scale-[0.98]"
                    :class="project === 'ejec'
                        ? 'bg-gradient-to-r from-blue-500 to-blue-600 shadow-lg shadow-blue-500/30 hover:from-blue-600 hover:to-blue-700'
                        : 'bg-gradient-to-r from-green-500 to-emerald-600 shadow-lg shadow-green-500/30 hover:from-green-600 hover:to-emerald-700'">
                ✍️ 记录 ${ project }
            </button>
        </form>

        <!-- 最近一周记录（分页） -->
        <div v-if="recentWeekList.length" class="mb-4 overflow-hidden rounded-2xl border border-blue-100 bg-blue-50">
            <div class="flex items-center justify-between gap-2 border-b border-blue-100 px-3 py-2.5">
                <h3 class="flex items-center gap-2 text-base font-semibold text-blue-700">
                    最近一周记录
                    <span class="rounded-full bg-blue-200 px-2 py-0.5 text-xs text-blue-800">${ recentWeekList.length } 条</span>
                </h3>
            </div>

            <ul class="divide-y divide-blue-100">
                <li v-for="item in pagedWeekList" :key="item.id || item._time"
                    class="flex items-start gap-2 px-3 py-2.5">
                    <span class="w-16 shrink-0 truncate text-sm font-bold sm:w-20"
                          :class="item.project === project ? 'text-blue-600' : 'text-gray-700'">${ item.project }</span>
                    <div class="min-w-0 flex-1">
                        <div class="flex flex-wrap items-center gap-x-2">
                            <span v-if="item.count" class="font-mono text-sm font-bold text-blue-700">${ item.count }次</span>
                            <span class="font-mono text-xs text-gray-400">${ item.date.slice(5) } ${ item.time.slice(0,5) }</span>
                        </div>
                        <div v-if="item.remark" class="mt-0.5 truncate text-xs text-gray-500">📝 ${ item.remark }</div>
                    </div>
                    <button v-if="isToday(item.date)" @click="deleteRecord(item.id)"
                            class="shrink-0 rounded-lg bg-red-50 px-2.5 py-1.5 text-xs text-red-500 transition hover:bg-red-500 hover:text-white active:scale-95">
                        删除
                    </button>
                </li>
            </ul>

            <!-- 翻页控件 -->
            <div v-if="totalPages > 1"
                 class="flex items-center justify-between gap-2 border-t border-blue-100 bg-blue-100/40 px-3 py-2">
                <button @click="goPage(currentPage - 1)" :disabled="currentPage <= 1"
                        class="rounded-lg bg-white px-3 py-2 text-sm font-medium text-blue-600 shadow-sm transition active:scale-95 disabled:opacity-40 disabled:active:scale-100">
                    ‹ 上一页
                </button>
                <span class="text-xs font-medium text-gray-500">
                    第 <b class="text-blue-600">${ currentPage }</b> / ${ totalPages } 页
                </span>
                <button @click="goPage(currentPage + 1)" :disabled="currentPage >= totalPages"
                        class="rounded-lg bg-white px-3 py-2 text-sm font-medium text-blue-600 shadow-sm transition active:scale-95 disabled:opacity-40 disabled:active:scale-100">
                    下一页 ›
                </button>
            </div>
        </div>

        <!-- 整体趋势图 -->
        <div class="mb-4">
            <h3 class="mb-1 text-sm font-semibold text-gray-600">📈 整体趋势（全部日期）</h3>
            <div class="relative h-56 sm:h-72">
                <canvas id="overallChart"></canvas>
            </div>
        </div>

        <!-- 最近趋势图 -->
        <div class="mb-4">
            <h3 class="mb-1 text-sm font-semibold text-gray-600">📉 最近趋势（最近30天）</h3>
            <div v-if="recentIntervalList.length" class="mb-1 text-xs text-gray-500">
                最近间隔：<span v-for="(interval, idx) in recentIntervalList" :key="idx" class="mr-1">${ interval }天</span>
            </div>
            <div class="relative h-56 sm:h-72">
                <canvas id="recentChart"></canvas>
            </div>
        </div>

        <!-- 主统计卡片 -->
        <div v-if="overallStats" class="mb-4 rounded-2xl border border-gray-200 bg-gray-50 p-4">
            <h3 class="mb-2 text-lg font-semibold text-gray-700">📊 统计数据（${ project }）</h3>
            <div class="grid grid-cols-2 gap-2 text-sm">
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">总记录条数：</span>
                    <span class="font-bold text-blue-600">${ overallStats.totalRecords }</span>
                </div>
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">有记录天数：</span>
                    <span class="font-bold">${ overallStats.activeDays } 天</span>
                </div>
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">整体频率：</span>
                    <span class="font-bold">${ formatFrequency(overallStats.overallPerDay) }</span>
                </div>
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">最近7天频率：</span>
                    <span class="font-bold">${ formatFrequency(overallStats.recentPerDay) }</span>
                    <span class="text-gray-500">（${ overallStats.recentRecords } 条）</span>
                </div>
            </div>
        </div>
        <div v-else class="mb-4 text-sm text-gray-400">暂无数据</div>

        <!-- 单次执行特点 -->
        <div v-if="overallStats && overallStats.isCountProject"
             class="mb-4 rounded-2xl border border-yellow-200 bg-yellow-50 p-4">
            <h3 class="mb-2 text-lg font-semibold text-yellow-800">⚡ 单次执行特点（基于 count 字段）</h3>
            <div class="grid grid-cols-2 gap-2 text-sm">
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">总执行次数：</span>
                    <span class="font-bold">${ overallStats.totalCountSum }</span>
                </div>
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">平均每次执行：</span>
                    <span class="font-bold">${ Math.round(overallStats.avgCountPerRecord) } 次</span>
                </div>
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">有记录日平均总次数：</span>
                    <span class="font-bold">${ Math.round(overallStats.avgCountPerActiveDay) } 次/天</span>
                </div>
                <div class="rounded-xl bg-white p-2.5 shadow-sm">
                    <span class="text-gray-500">平均每天记录条数：</span>
                    <span class="font-bold">${ Math.round(overallStats.avgRecordsPerActiveDay) } 条/天</span>
                </div>
            </div>
        </div>

    </div>
</div>

<script>
const { createApp } = Vue;
createApp({
    delimiters: ['${', '}'],
    data() {
        return {
            project: "downm",
            count: 6,
            remark: "",
            stats: [],
            squatOptions: [4, 5, 6, 7],
            allProjects: ["downm", "ejec", "mountain-15"],
            // 分页
            currentPage: 1,
            pageSize: 5
        }
    },
    computed: {
        projectStats() {
            return this.stats.filter(i => i.project === this.project);
        },
        recentWeekList() {
            const today = this.localToday();
            const start = new Date();
            start.setDate(start.getDate() - 6);
            const startStr = this.formatDate(start);
            return this.stats
                .filter(i => i.project === this.project && i.date >= startStr && i.date <= today)
                .sort((a, b) => `${b.date} ${b.time}`.localeCompare(`${a.date} ${a.time}`));
        },
        // 当前页的数据
        pagedWeekList() {
            const start = (this.currentPage - 1) * this.pageSize;
            return this.recentWeekList.slice(start, start + this.pageSize);
        },
        totalPages() {
            return Math.max(1, Math.ceil(this.recentWeekList.length / this.pageSize));
        },
        overallStats() {
            const data = this.projectStats;
            if (!data.length) return null;

            const dates = [...new Set(data.map(i => i.date))].sort();
            const first = dates[0];
            const last = dates[dates.length - 1];
            const totalDays = Math.floor((this.parseDateUTC(last) - this.parseDateUTC(first)) / 86400000) + 1;
            const activeDays = dates.length;

            const isCountProject = data.some(i => i.count !== null && i.count !== undefined);
            const totalRecords = data.length;
            const totalCountSum = isCountProject
                ? data.reduce((sum, i) => sum + (i.count || 0), 0)
                : totalRecords;

            const overallPerDay = totalRecords / totalDays;

            const today = this.localToday();
            const start7 = new Date();
            start7.setDate(start7.getDate() - 6);
            const start7Str = this.formatDate(start7);
            const recent = data.filter(i => i.date >= start7Str && i.date <= today);
            const recentRecords = recent.length;
            const recentPerDay = recentRecords / 7;

            const avgCountPerRecord = isCountProject ? totalCountSum / totalRecords : 1;
            const avgRecordsPerActiveDay = totalRecords / activeDays;
            const avgCountPerActiveDay = isCountProject ? totalCountSum / activeDays : 1;

            return {
                totalRecords,
                totalCountSum,
                activeDays,
                totalDays,
                overallPerDay,
                recentRecords,
                recentPerDay,
                isCountProject,
                avgCountPerRecord,
                avgRecordsPerActiveDay,
                avgCountPerActiveDay,
                first,
                last
            };
        },
        recentIntervalList() {
            const data = this.projectStats;
            if (!data.length) return [];
            const today = this.localToday();
            const start = new Date();
            start.setDate(start.getDate() - 29);
            const startStr = this.formatDate(start);
            const dates = [...new Set(data
                .filter(i => i.date >= startStr && i.date <= today)
                .map(i => i.date))]
                .sort();
            if (dates.length < 2) return [];
            const intervals = [];
            for (let i = 1; i < dates.length; i++) {
                const diff = (this.parseDateUTC(dates[i]) - this.parseDateUTC(dates[i - 1])) / 86400000;
                intervals.push(diff);
            }
            return intervals;
        }
    },
    watch: {
        project() {
            this.currentPage = 1;
            this.$nextTick(() => this.drawCharts());
        },
        // 数据变化导致总页数变少时，自动回到最后一页
        totalPages() {
            if (this.currentPage > this.totalPages) this.currentPage = this.totalPages;
        }
    },
    methods: {
        goPage(p) {
            if (p < 1 || p > this.totalPages) return;
            this.currentPage = p;
        },
        formatDate(date) {
            const y = date.getFullYear();
            const m = String(date.getMonth() + 1).padStart(2, '0');
            const d = String(date.getDate()).padStart(2, '0');
            return `${y}-${m}-${d}`;
        },
        localToday() {
            return this.formatDate(new Date());
        },
        isToday(dateStr) {
            return dateStr === this.localToday();
        },
        parseDateUTC(dateStr) {
            const [y, m, d] = dateStr.split('-').map(Number);
            return Date.UTC(y, m - 1, d);
        },
        generateDateRange(start, end) {
            const arr = [];
            const [sy, sm, sd] = start.split('-').map(Number);
            const [ey, em, ed] = end.split('-').map(Number);
            let cur = new Date(sy, sm - 1, sd);
            const last = new Date(ey, em - 1, ed);
            while (cur <= last) {
                arr.push(this.formatDate(cur));
                cur.setDate(cur.getDate() + 1);
            }
            return arr;
        },
        formatFrequency(perDay) {
            if (!perDay || perDay === 0) return '暂无';
            if (perDay >= 1) {
                return `平均每天 ${Math.round(perDay)} 次`;
            } else {
                const interval = 1 / perDay;
                return `平均每 ${Math.round(interval)} 天一次`;
            }
        },
        exportCSV() {
            window.location.href = API_BASE + '/daily_stats/export';
        },
        async importCSV(event) {
            const file = event.target.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);

            try {
                const res = await axios.post(API_BASE + '/api/daily_stats/import', formData);
                alert(`成功导入 ${res.data.imported_rows} 条记录`);
                this.$refs.fileInput.value = '';
                await this.fetchStats();
            } catch {
                alert("导入失败，请检查 CSV 格式或网络");
            }
        },
        async fetchStats() {
            try {
                const res = await axios.get(API_BASE + '/api/daily_stats');
                this.stats = (res.data || []).map(item => {
                    const [date = '1970-01-01', time = '00:00:00'] = (item._time || '').split(' ');
                    const count = item.count === '' || item.count == null ? null : Number(item.count);
                    return { ...item, date, time, count: Number.isFinite(count) ? count : null };
                }).sort((a, b) =>
                    `${b.date} ${b.time}`.localeCompare(`${a.date} ${a.time}`) || b.project.localeCompare(a.project)
                );

                [...new Set(this.stats.map(i => i.project))].forEach(p => {
                    if (p && !this.allProjects.includes(p)) this.allProjects.push(p);
                });

                this.drawCharts();
            } catch (error) {
                console.error("获取数据失败:", error);
            }
        },
        async addRecord() {
            if (!this.project) return;
            const payload = { project: this.project, remark: this.remark };
            if (this.project === 'ejec') payload.count = this.count;

            try {
                await axios.post(API_BASE + '/api/daily_stats', payload);
                this.remark = "";
                if (this.project === 'ejec') this.count = 6;
                await this.fetchStats();
                this.currentPage = 1;   // 新增记录后回到第一页查看最新
            } catch {
                alert("记录添加失败");
            }
        },
        async quickSquat(n) {
            this.count = n;
            this.remark = "";
            await this.addRecord();
        },
        drawCharts() {
            if (window.overallChart?.destroy) window.overallChart.destroy();
            if (window.recentChart?.destroy) window.recentChart.destroy();

            const projectData = this.projectStats;
            const canvasOverall = document.getElementById('overallChart');
            const canvasRecent = document.getElementById('recentChart');
            if (!canvasOverall || !canvasRecent) return;

            const ctxOverall = canvasOverall.getContext('2d');
            const ctxRecent = canvasRecent.getContext('2d');

            if (!projectData.length) {
                ctxOverall.clearRect(0, 0, canvasOverall.width, canvasOverall.height);
                ctxRecent.clearRect(0, 0, canvasRecent.width, canvasRecent.height);
                window.overallChart = null;
                window.recentChart = null;
                return;
            }

            const isEjec = this.project === 'ejec';
            const totalsByDate = new Map();
            projectData.forEach(record => {
                const amount = isEjec ? (record.count || 0) : 1;
                totalsByDate.set(record.date, (totalsByDate.get(record.date) || 0) + amount);
            });
            const sortedDates = [...totalsByDate.keys()].sort();
            const allDates = this.generateDateRange(sortedDates[0], sortedDates[sortedDates.length - 1]);

            const overallData = allDates.map(d => totalsByDate.has(d) ? totalsByDate.get(d) : null);

            const today = this.localToday();
            const start30 = new Date();
            start30.setDate(start30.getDate() - 29);
            const start30Str = this.formatDate(start30);
            const recentDates = this.generateDateRange(start30Str, today);
            const recentData = recentDates.map(d => totalsByDate.has(d) ? totalsByDate.get(d) : null);

            window.overallChart = new Chart(ctxOverall, {
                type: 'line',
                data: {
                    labels: allDates,
                    datasets: [{
                        label: `${this.project} ${isEjec ? '每日总次数' : '每日记录次数'}`,
                        data: overallData,
                        borderColor: isEjec ? '#3b82f6' : '#10b981',
                        backgroundColor: isEjec ? 'rgba(59, 130, 246, 0.1)' : 'rgba(16, 185, 129, 0.1)',
                        borderWidth: 2,
                        tension: 0.3,
                        fill: true,
                        spanGaps: false
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        tooltip: {
                            callbacks: {
                                label: (ctx) => ctx.raw === null ? '无记录' : `${ctx.raw} 次`
                            }
                        }
                    },
                    scales: {
                        y: { beginAtZero: true, ticks: { precision: 0 } },
                        x: { ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 20 } }
                    }
                }
            });

            const intervalMap = {};
            const activeRecentDates = recentDates.filter(d => totalsByDate.has(d));
            activeRecentDates.forEach((d, idx) => {
                if (idx > 0) {
                    const diff = (this.parseDateUTC(d) - this.parseDateUTC(activeRecentDates[idx - 1])) / 86400000;
                    intervalMap[d] = diff;
                }
            });

            window.recentChart = new Chart(ctxRecent, {
                type: 'line',
                data: {
                    labels: recentDates,
                    datasets: [{
                        label: `${this.project} ${isEjec ? '每日总次数' : '每日记录次数'}`,
                        data: recentData,
                        borderColor: isEjec ? '#3b82f6' : '#10b981',
                        backgroundColor: isEjec ? 'rgba(59, 130, 246, 0.1)' : 'rgba(16, 185, 129, 0.1)',
                        borderWidth: 2,
                        tension: 0.3,
                        fill: true,
                        spanGaps: false
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        tooltip: {
                            callbacks: {
                                title: (ctx) => `日期：${ctx[0].label}`,
                                label: (ctx) => {
                                    if (ctx.raw === null) return '无记录';
                                    let label = `${ctx.raw} 次`;
                                    const date = ctx.label;
                                    if (intervalMap[date] !== undefined) {
                                        label += `，距上次间隔 ${intervalMap[date]} 天`;
                                    }
                                    return label;
                                }
                            }
                        }
                    },
                    scales: {
                        y: { beginAtZero: true, ticks: { precision: 0 } },
                        x: { ticks: { maxRotation: 45, autoSkip: true, maxTicksLimit: 15 } }
                    }
                }
            });
        },
        async deleteRecord(id) {
            if (!id) return alert("该记录没有后端 ID，无法删除。请检查后端 API 返回格式。");
            if (confirm("确定删除这条记录吗？")) {
                try {
                    await axios.delete(API_BASE + `/api/daily_stats/${id}`);
                    await this.fetchStats();
                } catch {
                    alert("删除失败");
                }
            }
        }
    },
    mounted() {
        this.fetchStats();
    },
    beforeUnmount() {
        window.overallChart?.destroy?.();
        window.recentChart?.destroy?.();
        window.overallChart = null;
        window.recentChart = null;
    }
}).mount('#app');
</script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
</html>
''',
    'fuel': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <title>油耗统计</title>
    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <script src="static/cdn/axios1.1.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="static/cdn/chart.min.3.9.1.js"></script>
    <script src="static/cdn/chartjs-plugin-datalabels@2.js"></script>
    <!-- <script src="https://unpkg.com/vue@3.5.13/dist/vue.global.js"></script>
    <script src="https://unpkg.com/axios@1.1.3/dist/axios.min.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/chart.js@3.9.1/dist/chart.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2"></script> -->

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
<body class="bg-gray-50 min-h-screen">
<div id="app" class="max-w-6xl mx-auto mt-8 p-6 bg-white rounded-lg shadow">
    <div class="flex justify-between items-center mb-4">
        <h2 class="text-2xl font-bold text-blue-700">油耗统计</h2>
       
    </div>
    
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6 p-4 bg-gray-50 rounded-lg">
       

        <div class="text-center">
            <div class="text-2xl font-bold text-blue-600">${lastEfficiency} km</div>
            <div class="text-sm text-gray-600">最近油耗</div>
        </div>
        <div class="text-center">
            <div class="text-2xl font-bold text-blue-600">${totalMileage} km</div>
            <div class="text-sm text-gray-600">总里程</div>
        </div>
        <div class="text-center">
            <div class="text-2xl font-bold text-green-600">${totalFuel} L</div>
            <div class="text-sm text-gray-600">总加油量</div>
        </div>
        <div class="text-center">
            <div class="text-2xl font-bold text-purple-600">${avgEfficiency}</div>
            <div class="text-sm text-gray-600">平均油耗(L/100km)</div>
        </div>
        <div class="text-center">
            <div class="text-2xl font-bold text-orange-600">${recentTrend}</div>
            <div class="text-sm text-gray-600">最近趋势</div>
        </div>
    </div>
    
    <form @submit.prevent="addRecord" class="flex flex-col gap-3 mb-6 bg-blue-50 p-4 rounded-lg">
        <div class="flex flex-wrap gap-3 items-center">
            <span class="text-sm font-semibold text-blue-700">总里程(km)</span>
            <input type="number" v-model.number="totalMileageInput" step="0.1" min="0" class="w-24 px-2 py-1 border rounded" required>
            <span class="text-sm font-semibold text-green-700">加油量(L)</span>
            <input type="number" v-model.number="fuelAmount" step="0.1" min="0" class="w-24 px-2 py-1 border rounded" required>
            <input v-model="remark" class="flex-1 px-2 py-1 border rounded min-w-[200px]" placeholder="备注（可选）">
            <button type="submit" class="px-6 py-1 bg-blue-500 text-white font-bold rounded hover:bg-blue-600 transition">记录</button>
        </div>
    </form>
    
    <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
        <div>
            <h3 class="text-lg font-semibold mb-2 text-center text-gray-700">油耗趋势图</h3>
            <canvas id="fuelChart" style="height: 40vh; max-height: 350px;"></canvas>
        </div>
        <div>
            <h3 class="text-lg font-semibold mb-2 text-center text-gray-700">里程 vs 油耗散点图</h3>
            <canvas id="distanceChart" style="height: 40vh; max-height: 350px;"></canvas>
        </div>
    </div>

    <div class="mb-4">
        <h3 class="text-lg font-semibold mb-2 text-gray-700">历史记录</h3>
        <div class="overflow-x-auto">
            <table class="min-w-full table-auto text-sm text-center">
                <thead>
                    <tr class="bg-gray-200 text-gray-700">
                        <th class="px-4 py-2">日期时间</th>
                        <th class="px-4 py-2">总里程(km)</th>
                        <th class="px-4 py-2">加油量(L)</th>
                        <th class="px-4 py-2">单次里程(km)</th>
                        <th class="px-4 py-2">间隔天数</th>
                        <th class="px-4 py-2">日均里程(km)</th>
                        <th class="px-4 py-2 text-red-600">油耗(L/100km)</th>
                        <th class="px-4 py-2">备注</th>
                        <th class="px-4 py-2">操作</th>
                    </tr>
                </thead>
                <tbody>
                    <tr v-for="item in reversedStats" :key="item.id" class="border-b hover:bg-gray-50">
                        <td class="px-4 py-2">${item.date} <span class="text-gray-400 text-xs">${item.time}</span></td>
                        <td class="px-4 py-2 font-mono">${item.total_mileage}</td>
                        <td class="px-4 py-2 font-mono">${item.fuel_amount}</td>
                        <td class="px-4 py-2 font-mono text-gray-600">${item.mileage_diff}</td>
                        <td class="px-4 py-2 font-mono text-gray-600">${item.days_diff}</td>
                        <td class="px-4 py-2 font-mono text-gray-600">${item.daily_distance}</td>
                        <td class="px-4 py-2 font-bold" :class="item.fuel_efficiency === '--' ? 'text-gray-400' : 'text-red-500'">
                            ${item.fuel_efficiency}
                        </td>
                        <td class="px-4 py-2 text-gray-500 text-left max-w-[200px] truncate" :title="item.remark">${item.remark}</td>
                        <td class="px-4 py-2">
                            <button v-if="isDeletable(item._time)" @click="deleteRecord(item.id)" class="px-2 py-1 bg-red-100 text-red-700 rounded hover:bg-red-200 text-xs transition">删除</button>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>
     <div class="flex gap-2">
            <button @click="exportCSV" class="px-3 py-1 bg-green-500 text-white rounded hover:bg-green-600 text-sm transition">导出 CSV</button>
            <input type="file" ref="fileInput" @change="importCSV" class="hidden" accept=".csv">
            <button @click="$refs.fileInput.click()" class="px-3 py-1 bg-yellow-500 text-white rounded hover:bg-yellow-600 text-sm transition">导入 CSV</button>
        </div>
</div>

<script>
if (typeof ChartDataLabels !== 'undefined') {
    Chart.register(ChartDataLabels);
}

const { createApp } = Vue;

createApp({
    delimiters: ['${', '}'],
    data() {
        return {
            totalMileageInput: null,
            fuelAmount: null,
            remark: "",
            rawStats: [] // 存储后端返回的原始数据
        }
    },
    computed: {
        // 1. 获取最近一次的油耗数值
        lastEfficiency() {
            // 从处理好的数据中过滤出有油耗的数据
            const validList = this.processedStats.filter(item => item.fuel_efficiency !== '--');
            if (validList.length === 0) return '--';
            // 返回最后一条（也就是时间轴上最近的一条）
            return validList[validList.length - 1].fuel_efficiency;
        },
        // 【核心】前端数据处理引擎
        processedStats() {
            if (!this.rawStats || this.rawStats.length === 0) return [];
            
            // 1. 确保数据按时间升序排列，才能正确计算“差值”
            const sorted = [...this.rawStats].sort((a, b) => new Date(a._time) - new Date(b._time));
            
            // 2. 遍历并计算二级数据
            return sorted.map((item, index) => {
                let mileage_diff = '--';
                let days_diff = '--';
                let fuel_efficiency = '--';
                let daily_distance = '--';
                
                // 处理时间拆分
                const dateTimeStr = item._time || '';
                const dateStr = dateTimeStr.split(' ')[0] || '';
                const timeStr = dateTimeStr.split(' ')[1] || '';
                
                // 如果不是第一条记录，则计算差值
                if (index > 0) {
                    const prev = sorted[index - 1];
                    const currMileage = parseFloat(item.total_mileage) || 0;
                    const prevMileage = parseFloat(prev.total_mileage) || 0;
                    const currFuel = parseFloat(item.fuel_amount) || 0;
                    
                    const diffM = currMileage - prevMileage;
                    if (diffM > 0) {
                        mileage_diff = diffM.toFixed(1);
                        fuel_efficiency = ((currFuel / diffM) * 100).toFixed(2);
                    }
                    
                    // 计算天数差
                    const currDateObj = new Date(dateTimeStr);
                    const prevDateObj = new Date(prev._time);
                    const timeDiff = currDateObj.getTime() - prevDateObj.getTime();
                    const diffDays = timeDiff / (1000 * 3600 * 24);
                    
                    if (diffDays >= 0) {
                        // 保留一位小数，如果不足0.1天则按实际算或显示0
                        days_diff = diffDays.toFixed(1);
                        if (diffM > 0 && diffDays > 0) {
                            daily_distance = (diffM / diffDays).toFixed(1);
                        } else if (diffM > 0 && diffDays === 0) {
                            daily_distance = diffM.toFixed(1); // 同一天录入的距离直接作为日距
                        }
                    }
                }
                
                return {
                    ...item,
                    date: dateStr,
                    time: timeStr,
                    mileage_diff,
                    days_diff,
                    fuel_efficiency,
                    daily_distance
                };
            });
        },
        // 表格展示使用倒序（最新记录在最上面）
        reversedStats() {
            return [...this.processedStats].reverse();
        },
        
        // 顶部统计面板计算
        totalMileage() {
            if (this.rawStats.length === 0) return 0;
            return Math.max(...this.rawStats.map(item => parseFloat(item.total_mileage) || 0));
        },
        totalFuel() {
            return this.rawStats.reduce((sum, item) => sum + (parseFloat(item.fuel_amount) || 0), 0).toFixed(2);
        },
        avgEfficiency() {
            const validList = this.processedStats.filter(item => item.fuel_efficiency !== '--');
            if (validList.length === 0) return '0.00';
            const sum = validList.reduce((acc, item) => acc + parseFloat(item.fuel_efficiency), 0);
            return (sum / validList.length).toFixed(2);
        },
        recentTrend() {
            const validList = this.processedStats.filter(item => item.fuel_efficiency !== '--');
            if (validList.length < 2) return '数据不足';
            
            const last = parseFloat(validList[validList.length - 1].fuel_efficiency);
            const prev = parseFloat(validList[validList.length - 2].fuel_efficiency);
            const avg = parseFloat(this.avgEfficiency);
            
            let trend = last > avg + 0.05 ? '高于平均，' : last < avg - 0.05 ? '低于平均，' : '等于平均，';
            trend += last > prev + 0.05 ? '高于上次' : last < prev - 0.05 ? '低于上次' : '等于上次';
            return trend;
        }
    },
    methods: {
        // 判断是否为最近 3 天内的记录
        isDeletable(timeStr) {
            if (!timeStr) return false;
            // 处理后端传来的如 2026-03-19 21:46:32，去掉中间的横杠影响部分旧版浏览器解析
            const recordTime = new Date(timeStr.replace(/-/g, '/')).getTime(); 
            const now = new Date().getTime();
            // 3天的毫秒数: 3 * 24 * 60 * 60 * 1000 = 259200000
            return (now - recordTime) <= 259200000;
        },
        async fetchStats() {
            try {
                const res = await axios.get(API_BASE + '/api/fuel_stats');
                // 将后端数据直接存入 rawStats
                this.rawStats = res.data;
                this.$nextTick(() => {
                    this.drawChart();
                });
            } catch (error) {
                console.error("获取数据失败:", error);
            }
        },
        async addRecord() {
            if (!this.totalMileageInput || !this.fuelAmount) return;
            const payload = {
                total_mileage: this.totalMileageInput,
                fuel_amount: this.fuelAmount,
                remark: this.remark
            };
            try {
                await axios.post(API_BASE + '/api/fuel_stats', payload);
                this.totalMileageInput = null;
                this.fuelAmount = null;
                this.remark = "";
                await this.fetchStats();
            } catch (error) {
                alert("添加记录失败");
            }
        },
        async deleteRecord(id) {
            if (!id) {
                alert("该记录没有ID，无法通过接口删除");
                return;
            }
            if (confirm('确定删除这条记录吗？')) {
                try {
                    await axios.delete(API_BASE + `/api/fuel_stats/${id}`);
                    await this.fetchStats();
                } catch (error) {
                    alert("删除失败");
                }
            }
        },
        exportCSV() {
            window.location.href = API_BASE + '/fuel_stats/export';
        },
        async importCSV(event) {
            const file = event.target.files[0];
            if (!file) return;
            const formData = new FormData();
            formData.append('file', file);
            try {
                await axios.post(API_BASE + '/api/fuel_stats/import', formData, {
                    headers: { 'Content-Type': 'multipart/form-data' }
                });
                this.$refs.fileInput.value = ''; 
                await this.fetchStats();
            } catch (error) {
                alert("导入失败，请检查 CSV 格式");
            }
        },
        drawChart() {
            const validStats = this.processedStats.filter(item => item.fuel_efficiency !== '--');
            if (validStats.length === 0) return;

            // 折线图
            const ctxFuel = document.getElementById('fuelChart').getContext('2d');
            if (window.fuelChart instanceof Chart) window.fuelChart.destroy();
            window.fuelChart = new Chart(ctxFuel, {
                type: 'line',
                data: {
                    labels: validStats.map(item => item.date),
                    datasets: [{
                        label: '油耗 (L/100km)',
                        data: validStats.map(item => parseFloat(item.fuel_efficiency)),
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        tension: 0.3,
                        fill: true
                    }]
                },
                options: { responsive: true, plugins: { datalabels: { display: false } } }
            });

            // 散点图
            const ctxDist = document.getElementById('distanceChart').getContext('2d');
            if (window.distanceChart instanceof Chart) window.distanceChart.destroy();
            const scatterData = validStats.map(item => ({
                x: parseFloat(item.total_mileage),
                y: parseFloat(item.fuel_efficiency),
                remark: item.remark
            }));
            window.distanceChart = new Chart(ctxDist, {
                type: 'scatter',
                data: {
                    datasets: [{
                        label: '单点表现',
                        data: scatterData,
                        backgroundColor: '#10b981',
                        pointRadius: 6,
                        pointHoverRadius: 8
                    }]
                },
                options: {
                    responsive: true,
                    scales: {
                        x: { title: { display: true, text: '总里程 (km)' } },
                        y: { title: { display: true, text: '油耗 (L/100km)' } }
                    },
                    plugins: {
                        datalabels: { display: false },
                        tooltip: {
                            callbacks: {
                                label: function(context) {
                                    const point = context.raw;
                                    return [
                                        `里程: ${point.x} km`,
                                        `油耗: ${point.y} L/100km`,
                                        point.remark ? `备注: ${point.remark}` : ''
                                    ].filter(Boolean);
                                }
                            }
                        }
                    }
                }
            });
        }
    },
    mounted() {
        this.fetchStats();
    }
}).mount('#app');
</script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'history': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>时光切片档案</title>
    <link href="static/cdn/tailwind.min.css" rel="stylesheet">
    <link rel="stylesheet" href="static/cdn/viewer.min.css">
    <style>
        /* 基础视图样式 */
        .manual-img { cursor: zoom-in; transition: transform .2s; }
        .manual-img:hover { transform: scale(1.02); }
        .viewer-container img { cursor: zoom-in !important; }
        .viewer-move { cursor: grab !important; }
        .viewer-move:active { cursor: grabbing !important; }
        
        /* 滚动条优化 */
        .custom-scrollbar::-webkit-scrollbar { height: 6px; width: 6px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }

        /* 移动端详情弹窗视图控制 */
        @media (max-width: 767px) {
            #modalBody[data-view="text"] #modalImgContainer,
            #modalBody[data-view="image"] #modalTextContainer { display: none; }
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
<body class="bg-gray-50 min-h-screen font-sans">
    <nav class="bg-white shadow-sm border-b border-gray-200 px-4 md:px-6 py-4 sticky top-0 z-40">
        <div class="max-w-7xl mx-auto flex flex-col md:flex-row md:items-center justify-between">
            <div class="flex items-center justify-between flex-1">
                <h1 class="text-xl font-bold text-gray-800 tracking-tight mr-6 whitespace-nowrap shrink-0">⏳ 时光记录</h1>
                <button onclick="UI.toggleModal('uploadModal', true)" class="bg-black hover:bg-gray-800 text-white text-sm font-bold py-2 px-4 rounded-full shrink-0 md:order-last ml-auto md:ml-0">
                    <svg class="w-4 h-4 inline" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4"/></svg>添加记录
                </button>
                <input type="text" id="searchInput" class="hidden md:block flex-1 max-w-xl mx-4 bg-gray-100 border-transparent focus:bg-white focus:border-gray-500 rounded-xl px-4 py-2.5 text-sm outline-none" placeholder="🔍 搜索事件..." oninput="UI.renderGallery(true)">
            </div>
        </div>
    </nav>

    <div class="max-w-7xl mx-auto px-4 md:px-6 pt-6">
        <div class="flex items-center justify-between mb-3 w-full overflow-hidden">
            <h2 class="text-xs font-bold text-gray-400 uppercase tracking-widest whitespace-nowrap overflow-hidden overflow-ellipsis min-w-0 flex-1 pr-2">近期活跃事件 (Top 8)</h2>
            <button onclick="Utils.$('searchInput').value='';UI.renderGallery(true)" class="text-[10px] text-blue-500 hover:underline shrink-0 whitespace-nowrap">查看全部</button>
        </div>
        <div id="eventStatsDashboard" class="flex space-x-3 overflow-x-auto no-scrollbar pb-3 snap-x"></div>
    </div>

    <main class="max-w-7xl mx-auto px-4 md:px-6 py-4">
        <div id="gallery" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 md:gap-6"></div>
        <div id="lazyLoadTrigger" class="w-full text-center text-xs text-gray-400 py-6 font-bold tracking-widest"></div>
    </main>

    <div id="uploadModal" class="fixed inset-0 bg-black bg-opacity-60 backdrop-blur-sm z-50 hidden flex items-center justify-center p-4" style="z-index: 70;">
        <div class="bg-white w-full max-w-md rounded-3xl overflow-hidden shadow-2xl">
            <div class="p-5 border-b flex justify-between items-center bg-gray-50">
                <h3 class="font-bold text-gray-800">📸 存档生活切片</h3>
                <button onclick="UI.toggleModal('uploadModal', false)" class="text-gray-400 text-2xl leading-none">&times;</button>
            </div>
            <div class="p-6">
                <div class="mb-4">
                    <label class="block text-xs font-bold text-gray-700 mb-1">记录事件 (同批次共用)</label>
                    <input type="text" id="eventType" class="w-full bg-gray-50 border border-gray-200 rounded-xl px-3 py-2 text-sm outline-none mb-2" placeholder="输入新事件，或点击下方历史标签">
                    <div id="quickSelectContainer" class="flex flex-wrap gap-2 max-h-32 overflow-y-auto custom-scrollbar"></div>
                </div>
                <div class="mb-4">
                    <label class="block text-xs font-bold text-gray-700 mb-1">实际发生时间</label>
                    <input type="datetime-local" id="overrideTime" class="w-full bg-gray-50 border border-gray-200 rounded-xl px-3 py-2 text-sm outline-none">
                </div>
                <div class="border-2 border-dashed border-gray-300 bg-gray-50 rounded-2xl p-8 text-center hover:bg-gray-100 mb-4 cursor-pointer" onclick="Utils.$('fileInput').click()">
                    <p class="text-sm font-semibold text-gray-700">点击选择多张照片</p>
                    <input type="file" id="fileInput" accept="image/*" multiple class="hidden" onchange="Action.handleFiles()">
                </div>
                <div id="previewContainer" class="hidden mb-4 rounded-xl overflow-x-auto border border-gray-200 p-2 flex gap-2 custom-scrollbar h-28 bg-gray-50"></div>
                <button id="btnSubmit" onclick="Action.submitRecord()" class="w-full bg-gray-300 text-gray-500 font-bold py-3.5 px-4 rounded-xl" disabled>等待选择照片...</button>
            </div>
        </div>
    </div>

    <div id="detailModal" class="fixed inset-0 bg-black bg-opacity-90 backdrop-blur-sm z-50 hidden flex items-center justify-center md:p-4" style="z-index: 80;">
        <div class="bg-white w-full h-full md:h-5/6 md:max-w-6xl md:rounded-3xl flex flex-col shadow-2xl">
            <div class="p-4 border-b flex justify-between items-center bg-gray-50 z-10">
                <h3 class="font-bold text-gray-800 flex items-center gap-2 pr-4 overflow-hidden">
                    <span id="modalEventTitle" class="truncate text-lg shrink-0 max-w-xs md:max-w-md"></span>
                    <span id="modalDate" class="text-xs font-normal text-gray-500 bg-gray-200 px-2 py-1 rounded shrink-0"></span>
                </h3>
                <button onclick="UI.toggleModal('detailModal', false)" class="text-gray-500 bg-gray-200 hover:bg-gray-300 rounded-full w-8 h-8 flex items-center justify-center transition-colors shrink-0">&times;</button>
            </div>
            <div class="md:hidden flex bg-gray-100 p-2 gap-2 border-b">
                <button onclick="UI.setModalView('text')" id="btnViewText" class="flex-1 py-1.5 bg-white shadow rounded-lg text-sm font-bold text-gray-800">📝 环境档案</button>
                <button onclick="UI.setModalView('image')" id="btnViewImg" class="flex-1 py-1.5 text-gray-500 hover:bg-gray-200 rounded-lg text-sm font-bold">🖼️ 视觉快照</button>
            </div>
            <div id="modalBody" data-view="image" class="flex-1 flex flex-col md:flex-row overflow-hidden bg-white">
                <div id="modalImgContainer" class="w-full md:w-1/2 lg:w-3/5 h-full bg-black flex items-center justify-center relative">
                    <img id="modalDetailImg" class="max-w-full max-h-full object-contain cursor-zoom-in">
                    <div id="modalFilename" class="absolute bottom-2 left-2 text-[10px] text-white text-opacity-50 bg-black bg-opacity-50 px-2 py-1 rounded select-all"></div>
                </div>
                <div id="modalTextContainer" class="w-full md:w-1/2 lg:w-2/5 h-full overflow-y-auto p-5 md:p-8 bg-gray-50">
                    <h4 class="text-xs font-bold text-gray-400 uppercase tracking-widest mb-4">Background Context</h4>
                    <div id="modalContent" class="prose prose-sm text-gray-700"></div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/exif-js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/viewerjs/1.11.3/viewer.min.js"></script>
 <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/exif-js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/viewerjs/1.11.3/viewer.min.js"></script>
    <script>
        // ================= 1. 配置与状态 =================
        const API_BASE = location.pathname.replace(/\/[^/]*$/, "");
        const State = { snapshots: [], viewer: null, modalViewer: null, displayLimit: 10, observer: null };

        // ================= 2. 核心工具库 =================
        const Utils = {
            $: id => document.getElementById(id),
            // 修复：后端传来的已经是本地时间，直接去掉横线冒号取前12位，避免 toISOString() 造成8小时时差
            formatDT: d => d ? d.replace('T', ' ').substring(0, 16) : '未知',
            timeAgo: d => {
                if (!d) return '未知';
                const diff = (Date.now() - new Date(d.replace(' ', 'T')).getTime()) / 864e5;
                return diff < 1 ? '今天' : diff < 2 ? '昨天' : diff > 365 ? `${~~(diff/365)}年前` : diff > 30 ? `${~~(diff/30)}个月前` : `${~~diff}天前`;
            },
            compressImage: file => new Promise((resolve, reject) => {
                if (file.size < 250 * 1024 && (file.type === 'image/jpeg' || file.type === 'image/webp')) {
                    return resolve(file);
                }
                const reader = new FileReader();
                reader.readAsDataURL(file);
                reader.onload = (event) => {
                    const img = new Image();
                    img.src = event.target.result;
                    img.onload = () => {
                        const maxEdge = 1280;
                        let width = img.width;
                        let height = img.height;

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

                        ctx.imageSmoothingEnabled = true;
                        ctx.imageSmoothingQuality = 'medium';
                        ctx.fillStyle = '#FFFFFF';
                        ctx.fillRect(0, 0, width, height);
                        ctx.drawImage(img, 0, 0, width, height);

                        canvas.toBlob((blob) => {
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
            })
        };

        // ================= 3. 接口请求层 =================
        const API = {
            load: async () => {
                State.snapshots = await (await fetch(API_BASE + '/api/snapshots')).json();
                UI.renderStats(); UI.renderGallery(true);
            },
            del: async (id, e) => {
                e.stopPropagation();
                if(confirm("永久删除此切片？")) { await fetch(API_BASE + `/api/snapshots/${id}`, {method: 'DELETE'}); API.load(); }
            },
            reqAI: async (id, e) => {
                e.stopPropagation();
                await fetch(API_BASE + '/api/snapshots/analyze', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id})});
                API.load();
            }
        };

        // ================= 4. UI 渲染层 =================
        const UI = {
            toggleModal: (id, show) => { Utils.$(id).classList.toggle('hidden', !show); document.body.style.overflow = show ? 'hidden' : 'auto'; },
            setModalView: v => {
                Utils.$('modalBody').setAttribute('data-view', v);
                const active = "flex-1 py-1.5 bg-white shadow rounded-lg text-sm font-bold text-gray-800",
                      normal = "flex-1 py-1.5 text-gray-500 hover:bg-gray-200 rounded-lg text-sm font-bold";
                Utils.$('btnViewImg').className = v === 'image' ? active : normal;
                Utils.$('btnViewText').className = v === 'text' ? active : normal;
            },
            renderStats: () => {
                const stats = {};
                State.snapshots.forEach(i => {
                    const t = i.event_type || '未命名'; if(!t.trim()) return;
                    const tm = new Date(i.record_time.replace(' ', 'T')).getTime();
                    if (!stats[t] || tm > stats[t].time) stats[t] = { time: tm, rt: i.record_time };
                });

                const sortedKeys = Object.keys(stats).sort((a, b) => stats[b].time - stats[a].time);
                const topDashboard = sortedKeys.slice(0, 8); 

                Utils.$('eventStatsDashboard').innerHTML = topDashboard.map(t => `
                    <div onclick="Utils.$('searchInput').value='${t}';UI.renderGallery(true)" class="bg-white px-4 py-2.5 rounded-2xl border border-gray-200 shadow-sm shrink-0 cursor-pointer hover:border-black hover:shadow-md group min-w-[100px] snap-center">
                        <span class="text-sm font-bold text-gray-800 group-hover:text-black">${t}</span>
                        <div class="text-[11px] text-gray-500 mt-0.5">上次: ${Utils.timeAgo(stats[t].rt)}</div>
                    </div>`).join('');

                Utils.$('quickSelectContainer').innerHTML = sortedKeys.map(t => `
                    <button onclick="Utils.$('eventType').value='${t}';Utils.$('fileInput').files.length && Action.submitRecord()" class="px-3 py-1.5 bg-gray-100 hover:bg-black hover:text-white text-gray-700 rounded-xl text-left">
                        <span class="text-[11px] font-bold">${t}</span><span class="text-[9px] opacity-70 ml-1">上次: ${Utils.timeAgo(stats[t].rt)}</span>
                    </button>`).join('');
            },
            renderGallery: (reset = false) => {
                if (reset) State.displayLimit = 10;
                
                const kw = Utils.$('searchInput').value.toLowerCase();
                const filtered = State.snapshots.filter(i => (i.event_type||'').toLowerCase().includes(kw) || (i.description||'').toLowerCase().includes(kw) || (i.original_filename||'').toLowerCase().includes(kw));
                
                const currentBatch = filtered.slice(0, State.displayLimit);

                const html = currentBatch.map(i => {
                    const isAi = i.status === 'analyzing_context', isErr = /失败|异常/.test(i.description);
                    const btn = isAi ? `<button disabled class="w-full py-2 bg-gray-100 text-gray-400 text-[11px] font-bold rounded-xl">🤖 分析中...</button>` 
                                     : (!i.description || isErr) ? `<button onclick="API.reqAI('${i.id}',event)" class="w-full py-2 ${isErr?'bg-red-50 text-red-600':'bg-indigo-50 text-indigo-600'} text-[11px] font-bold rounded-xl border">${isErr?'🤖 重新提取':'🤖 AI 提取环境'}</button>`
                                     : `<button onclick="Action.showDetail('${i.id}')" class="w-full py-2 bg-gray-50 hover:bg-gray-200 text-gray-700 text-[11px] font-bold rounded-xl border border-gray-200">查看档案</button>`;
                    return `
                    <div class="bg-white rounded-2xl shadow-sm border border-gray-200 overflow-hidden group relative">
                        <button onclick="API.del('${i.id}',event)" class="absolute top-2 left-2 z-10 opacity-0 group-hover:opacity-100 bg-white bg-opacity-90 shadow rounded-full p-2 text-red-500 hover:bg-red-50" title="删除">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
                        </button>
                        <div class="relative h-40 sm:h-52 bg-gray-100">
                            <img src="${API_BASE}${i.url}" class="w-full h-full object-cover manual-img">
                            ${isAi ? `<div class="absolute inset-0 bg-black bg-opacity-50 flex items-center justify-center text-xs text-white font-bold">🤖 分析中...</div>` : ''}
                            <div class="absolute bottom-0 inset-x-0 bg-gradient-to-t from-black to-transparent p-2 opacity-80"><div class="text-[10px] text-gray-200 font-mono">${Utils.formatDT(i.record_time)}</div></div>
                        </div>
                        <div class="p-3 sm:p-4 flex-1 flex flex-col">
                            <div class="text-sm font-bold text-gray-900 truncate mb-1">${i.event_type}</div>
                            <div class="flex flex-wrap gap-1 mb-3 h-5 overflow-hidden">${(i.tags||[]).map(t=>`<span class="bg-gray-100 text-gray-600 text-[10px] px-1.5 py-0.5 rounded border">${t}</span>`).join('')}</div>
                            ${btn}
                        </div>
                    </div>`;
                }).join('');

                Utils.$('gallery').innerHTML = html;
                
                if (State.viewer) State.viewer.destroy();
                State.viewer = new Viewer(Utils.$('gallery'), { filter: img => img.classList.contains('manual-img'), viewed() { this.viewer.canvas.addEventListener('click', e => { if(e.target===this.viewer.canvas) State.viewer.hide(); }); } });

                const trigger = Utils.$('lazyLoadTrigger');
                if (State.displayLimit < filtered.length) {
                    trigger.innerHTML = '滑动加载更多...';
                    if (!State.observer) {
                        State.observer = new IntersectionObserver((entries) => {
                            if (entries[0].isIntersecting) {
                                State.displayLimit += 10;
                                UI.renderGallery(false);
                            }
                        }, { rootMargin: "100px" });
                    }
                    State.observer.observe(trigger);
                } else {
                    trigger.innerHTML = filtered.length > 0 ? '到底啦' : '暂无数据';
                    if (State.observer) {
                        State.observer.disconnect();
                        State.observer = null;
                    }
                }
            }
        };

        // ================= 5. 交互动作层 =================
        const Action = {
            showDetail: id => {
                const i = State.snapshots.find(x => x.id === id);
                Utils.$('modalEventTitle').textContent = i.event_type;
                Utils.$('modalDate').textContent = Utils.formatDT(i.record_time);
                Utils.$('modalContent').innerHTML = marked.parse(i.description || "无描述");
                Utils.$('modalFilename').textContent = i.original_filename || '';
                Utils.$('modalFilename').classList.toggle('hidden', !i.original_filename);

                const img = Utils.$('modalDetailImg'); img.src = API_BASE + i.url;
                if(State.modalViewer) State.modalViewer.destroy();
                img.onload = () => { State.modalViewer = new Viewer(img, { inline: false, zIndex: 9999, navbar: false, title: false, toolbar: { zoomIn: 1, zoomOut: 1, oneToOne: 1, reset: 1, rotateLeft: 1, rotateRight: 1 }, zoomOnTouch: true, viewed() { this.viewer.canvas.addEventListener('click', e => { if(e.target===this.viewer.canvas) State.modalViewer.hide(); }); } }); };
                if(img.complete) img.onload();
                UI.setModalView('image'); UI.toggleModal('detailModal', true);
            },
            handleFiles: () => {
                const files = Utils.$('fileInput').files; if(!files.length) return;
                const ot = Utils.$('overrideTime');
                
                if(!ot.value && files[0]) { 
                    EXIF.getData(files[0], function() { 
                        let d;
                        const exifTime = EXIF.getTag(this, "DateTimeOriginal");
                        if (exifTime) {
                            // 修复 EXIF 冒号格式导致 invalid date，转为标准格式
                            const parts = exifTime.split(' ');
                            d = new Date(parts[0].replace(/:/g, '-') + 'T' + parts[1]);
                        } else {
                            d = new Date(files[0].lastModified);
                        }
                        // 修复：提取本地时间拼接，强制规避 toISOString 带来的时区偏移
                        const pad = n => n.toString().padStart(2, '0');
                        ot.value = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
                    }); 
                }
                
                Utils.$('previewContainer').innerHTML = Array.from(files).map(f => `<img src="${URL.createObjectURL(f)}" class="h-24 w-24 object-cover rounded shadow border">`).join('');
                Utils.$('previewContainer').classList.remove('hidden');
                
                const btn = Utils.$('btnSubmit');
                btn.disabled = false; btn.className = "w-full bg-black hover:bg-gray-800 text-white font-bold py-3.5 px-4 rounded-xl";
                btn.innerHTML = `✅ 立即入库 (${files.length} 张)`;
            },
            submitRecord: async () => {
                const files = Utils.$('fileInput').files, btn = Utils.$('btnSubmit'); if(!files.length) return;
                btn.disabled = true;
                const fd = new FormData(); fd.append('event_type', Utils.$('eventType').value); fd.append('record_time', Utils.$('overrideTime').value.replace('T', ' '));
                try {
                    for(let i=0; i<files.length; i++) {
                        btn.innerHTML = `处理中... (${i+1}/${files.length})`;
                        fd.set('file', await Utils.compressImage(files[i]));
                        await fetch(API_BASE + '/api/snapshots/upload', { method: 'POST', body: fd });
                    }
                    UI.toggleModal('uploadModal', false); API.load();
                } catch(e) { alert("保存失败: " + e.message); btn.disabled = false; btn.innerHTML = "✅ 继续入库"; }
            }
        };

        // ================= 6. 启动初始化 =================
        API.load();
    </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'audiom': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>VocalMind · 智能录音库</title>
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="default">
    <meta name="theme-color" content="#0f172a">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=DM+Mono:wght@400;500&family=Noto+Sans+SC:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/wavesurfer.js@7/dist/wavesurfer.min.js"></script>
    <script>
        tailwind.config={theme:{extend:{colors:{ink:{50:'#f8f6f2',100:'#f0ece4',200:'#e2dbd0',300:'#c9bfb0',400:'#a89a86',500:'#8a7b68',600:'#6b5e4e',700:'#50463a',800:'#362f28',900:'#1c1814'},tea:{50:'#effcf9',100:'#d7f7ef',200:'#b3efe0',300:'#7ae2cc',400:'#2dd4bf',500:'#14b8a6',600:'#0d9488',700:'#0f766e'}}}}}
    </script>
    <style>
        body{font-family:'Noto Sans SC',-apple-system,sans-serif;-webkit-overflow-scrolling:touch;background:#f0ece4;min-height:100vh;min-height:100dvh}
        body::before{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");background-size:256px;opacity:.025;pointer-events:none;z-index:0}
        .font-display{font-family:'Cormorant Garamond',Georgia,serif}
        .font-mono{font-family:'DM Mono','Menlo',monospace}
        .custom-scrollbar::-webkit-scrollbar{width:5px}.custom-scrollbar::-webkit-scrollbar-thumb{background:#c9bfb0;border-radius:4px}
        @keyframes fadeInUp{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
        @keyframes fadeIn{from{opacity:0}to{opacity:1}}
        @keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
        @keyframes pulseGlow{0%,100%{box-shadow:0 0 0 0 rgba(220,50,50,.3)}50%{box-shadow:0 0 0 14px rgba(220,50,50,0)}}
        @keyframes slideUp{from{opacity:0;transform:translateY(100%)}to{opacity:1;transform:translateY(0)}}
        @keyframes modalIn{from{opacity:0;transform:scale(.96) translateY(8px)}to{opacity:1;transform:scale(1) translateY(0)}}
        @keyframes toastIn{from{opacity:0;transform:translateX(120%)}to{opacity:1;transform:translateX(0)}}
        @keyframes toastOut{to{opacity:0;transform:translateX(120%)}}
        @keyframes dropBounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
        .anim-fade-in{animation:fadeIn .3s ease}
        .anim-fade-in-up{animation:fadeInUp .45s cubic-bezier(.16,1,.3,1) forwards}
        .anim-slide-up{animation:slideUp .35s cubic-bezier(.16,1,.3,1)}
        .anim-modal-in{animation:modalIn .3s cubic-bezier(.16,1,.3,1)}
        .anim-toast-in{animation:toastIn .35s cubic-bezier(.16,1,.3,1)}
        .anim-toast-out{animation:toastOut .25s ease-in forwards}
        .skeleton{background:linear-gradient(90deg,#e2dbd0 25%,#f0ece4 50%,#e2dbd0 75%);background-size:200% 100%;animation:shimmer 1.5s infinite;border-radius:8px}
        .recording-pulse{animation:pulseGlow 1.5s ease infinite}
        .drop-bounce{animation:dropBounce 1.2s ease-in-out infinite}
        .sync-text{transition:background-color .15s,color .15s;cursor:pointer;padding:3px 5px;margin:1px;border-radius:5px;display:inline;line-height:1.9;-webkit-tap-highlight-color:transparent}
        .sync-text:hover{background:rgba(45,212,191,.12)}
        .sync-text.active{background:rgba(45,212,191,.2);color:#0d9488;font-weight:600}
        .timeline-card{transition:transform .2s ease,box-shadow .2s ease}
        .timeline-card:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(0,0,0,.06)}
        .card-enter{animation:fadeInUp .4s cubic-bezier(.16,1,.3,1) forwards}
        .pb-safe{padding-bottom:env(safe-area-inset-bottom,0px)}
        .prog-track{transition:height .15s ease}
        .prog-wrap:hover .prog-track{height:8px}
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
<body class="text-ink-900 pb-safe">

<!-- ─── Nav ─── -->
<nav class="sticky top-0 z-40 bg-ink-900/[0.97] backdrop-blur-md shadow-lg shadow-ink-900/20">
    <div class="max-w-3xl mx-auto flex items-center gap-3 px-4 py-3">
        <div class="shrink-0 flex items-center gap-2.5">
            <div class="w-8 h-8 bg-tea-500 rounded-lg flex items-center justify-center">
                <svg class="w-4 h-4 text-white" fill="currentColor" viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5-3c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>
            </div>
            <span class="font-display text-lg font-bold text-ink-100 tracking-wide hidden sm:block">VocalMind</span>
        </div>
        <div class="flex-1 relative">
            <svg class="w-4 h-4 text-ink-500 absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35" stroke-linecap="round"/></svg>
            <input type="text" id="searchInput" class="w-full bg-ink-800 text-ink-200 placeholder-ink-500 rounded-xl pl-10 pr-4 py-2.5 text-sm outline-none focus:bg-ink-800/80 focus:ring-1 ring-tea-500/50 transition-all border border-ink-700/50" placeholder="搜索录音标题或内容..." oninput="handleSearch(this.value)">
        </div>
        <!-- 修改了导航栏按钮的功能：专注于上传文件，录音交给首页悬浮按钮 -->
        <button onclick="RecordCtrl.open()" class="bg-tea-500 hover:bg-tea-400 active:bg-tea-600 text-white rounded-xl px-4 py-2.5 font-semibold text-sm flex items-center gap-2 shrink-0 shadow-md shadow-tea-500/25 transition-all hover:shadow-lg">
            <svg class="w-4 h-4" fill="none" stroke="currentColor" stroke-width="2.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/></svg>
            <span class="hidden sm:block">上传</span>
        </button>
    </div>
</nav>

<!-- ─── Main ─── -->
<!-- 增加了 pb-32 以免底部悬浮按钮遮挡最后一条记录 -->
<main class="max-w-3xl mx-auto px-4 pt-6 pb-32 relative z-10">
    <div id="timeline" class="space-y-4"></div>
    <div id="emptyState" class="hidden py-20 text-center anim-fade-in-up">
        <div class="w-20 h-20 bg-ink-100 rounded-full flex items-center justify-center mx-auto mb-6 border border-ink-200/80">
            <svg class="w-9 h-9 text-ink-400" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 18.75a6 6 0 0 0 6-6v-1.5m-6 7.5a6 6 0 0 1-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 0 1-3-3V4.5a3 3 0 1 1 6 0v8.25a3 3 0 0 1-3 3Z"/></svg>
        </div>
        <h3 class="font-display text-2xl font-bold text-ink-700 mb-2">尚无录音记录</h3>
        <p class="text-ink-400 text-sm mb-3">点击底部麦克风开始录音，或将音频/视频文件拖拽到此页面</p>
    </div>
</main>

<!-- ─── 首页底部一键录音悬浮按钮 (FAB) ─── -->
<div class="fixed bottom-6 left-1/2 -translate-x-1/2 z-30 flex flex-col items-center pointer-events-none drop-shadow-2xl">
    <div id="quickRecTime" class="hidden mb-3 bg-ink-900/90 text-white font-mono text-sm px-5 py-1.5 rounded-full backdrop-blur-md pointer-events-auto shadow-lg border border-white/10 anim-fade-in">00:00</div>
    <button id="quickRecBtn" onclick="RecordCtrl.toggleRecord()" class="w-16 h-16 rounded-full bg-ink-900 text-white flex items-center justify-center pointer-events-auto transition-all duration-300 active:scale-90 border-[3px] border-white shadow-xl hover:bg-ink-800 relative group">
        <svg id="quickMicIcon" class="w-7 h-7" fill="currentColor" viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5-3c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg>
        <div id="quickRecDot" class="absolute top-0 right-0 w-3.5 h-3.5 bg-red-500 border-2 border-white rounded-full hidden"></div>
    </button>
</div>

<!-- ─── Detail Modal (transcript only) ─── -->
<div id="detailModal" class="fixed inset-0 z-50 hidden">
    <div class="absolute inset-0 bg-ink-900/60 backdrop-blur-sm anim-fade-in" onclick="DetailCtrl.close()"></div>
    <div class="absolute inset-0 flex flex-col justify-end sm:justify-center p-0 sm:p-4 pointer-events-none">
        <div class="bg-white w-full sm:max-w-2xl sm:mx-auto h-[85vh] sm:h-[80vh] rounded-t-3xl sm:rounded-2xl flex flex-col shadow-2xl overflow-hidden pointer-events-auto anim-modal-in">
            <div class="px-5 py-4 border-b border-ink-200/60 flex justify-between items-center shrink-0">
                <h3 id="modalTitle" class="font-display font-bold text-ink-800 truncate pr-6 text-lg flex-1"></h3>
                <button onclick="DetailCtrl.close()" class="text-ink-400 hover:text-ink-700 bg-ink-100 hover:bg-ink-200/80 rounded-full w-8 h-8 flex items-center justify-center transition-all shrink-0 text-lg leading-none">&times;</button>
            </div>
            <!-- Waveform player (secondary, for seeking) -->
            <div class="p-5 bg-ink-900 text-white flex flex-col gap-3 shrink-0 select-none">
                <div id="waveform" class="w-full h-12 touch-none rounded-lg overflow-hidden"></div>
                <div class="flex items-center justify-between gap-3">
                    <button id="playBtn" onclick="DetailCtrl.togglePlay()" class="bg-tea-500 hover:bg-tea-400 rounded-full w-10 h-10 flex items-center justify-center transition-all shadow-md shrink-0">
                        <svg id="playIcon" class="w-5 h-5 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
                    </button>
                    <div class="flex-1 h-1.5 bg-ink-700 rounded-full overflow-hidden cursor-pointer" id="progressBar">
                        <div id="progressFill" class="h-full bg-tea-500 rounded-full" style="width:0%;transition:width .1s linear"></div>
                    </div>
                    <span id="timeDisplay" class="text-xs font-mono text-ink-300 shrink-0 tabular-nums">00:00 / 00:00</span>
                </div>
            </div>
            <div class="flex-1 overflow-y-auto p-5 sm:p-6 custom-scrollbar text-ink-700 text-[15px] leading-[1.95]" id="syncTextContainer"></div>
        </div>
    </div>
</div>

<!-- ─── Upload Modal (简化为专用于上传) ─── -->
<div id="recordModal" class="fixed inset-0 z-50 hidden">
    <div class="absolute inset-0 bg-ink-900/60 backdrop-blur-sm anim-fade-in" onclick="RecordCtrl.close()"></div>
    <div class="absolute inset-x-0 bottom-0 sm:inset-0 sm:flex sm:items-center sm:justify-center sm:p-4 pointer-events-none">
        <div class="bg-white w-full sm:max-w-md sm:rounded-3xl rounded-t-3xl p-6 text-center shadow-2xl pointer-events-auto anim-slide-up relative overflow-hidden">
            <div class="absolute top-0 right-0 w-40 h-40 bg-tea-500/[0.04] rounded-full -translate-y-1/2 translate-x-1/2"></div>
            <div class="w-12 h-1.5 bg-ink-200 rounded-full mx-auto mb-6 sm:hidden"></div>
            <h3 class="font-display font-bold text-ink-800 text-xl mb-1">上传文件</h3>
            <p class="text-ink-400 text-xs mb-8">支持自动提取视频中的音频转录文字</p>

            <div class="flex gap-3">
                <label class="flex-1 py-4 bg-ink-50 hover:bg-ink-100 active:bg-ink-200/60 text-ink-600 font-semibold text-sm rounded-xl transition-colors cursor-pointer flex flex-col items-center justify-center gap-3 border border-ink-200/60 hover:border-ink-300">
                    <div class="w-10 h-10 rounded-full bg-white flex items-center justify-center shadow-sm">
                        <svg class="w-5 h-5 text-ink-500" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"/></svg>
                    </div>
                    上传音频
                    <input type="file" accept="audio/*" class="absolute w-0 h-0 opacity-0" onchange="if(this.files.length) RecordCtrl.handleUpload(this.files[0],0,false); this.value=''">
                </label>
                <label class="flex-1 py-4 bg-ink-50 hover:bg-ink-100 active:bg-ink-200/60 text-ink-600 font-semibold text-sm rounded-xl transition-colors cursor-pointer flex flex-col items-center justify-center gap-3 border border-ink-200/60 hover:border-ink-300">
                    <div class="w-10 h-10 rounded-full bg-white flex items-center justify-center shadow-sm">
                        <svg class="w-5 h-5 text-ink-500" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m15.75 10.5 4.72-4.72a.75.75 0 0 1 1.28.53v11.38a.75.75 0 0 1-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 0 0 2.25-2.25v-9a2.25 2.25 0 0 0-2.25-2.25h-9A2.25 2.25 0 0 0 2.25 7.5v9a2.25 2.25 0 0 0 2.25 2.25Z"/></svg>
                    </div>
                    上传视频
                    <input type="file" accept="video/*" class="absolute w-0 h-0 opacity-0" onchange="if(this.files.length) RecordCtrl.handleUpload(this.files[0],0,false); this.value=''">
                </label>
            </div>
            <button onclick="RecordCtrl.close()" class="w-full mt-6 py-2 text-ink-400 hover:text-ink-600 text-sm font-medium transition-colors">取消</button>
        </div>
    </div>
</div>

<!-- ─── Global Upload Overlay ─── -->
<div id="uploadOverlay" class="fixed inset-0 z-[100] bg-ink-900/40 backdrop-blur-sm flex flex-col items-center justify-center gap-4 hidden anim-fade-in">
    <div class="bg-white px-8 py-6 rounded-3xl shadow-2xl flex flex-col items-center gap-4">
        <div class="w-10 h-10 border-[3px] border-ink-200 border-t-tea-500 rounded-full animate-spin"></div>
        <p id="uploadStatusText" class="text-sm font-medium text-ink-600">正在处理并上传...</p>
    </div>
</div>

<!-- ─── Drag & Drop Overlay ─── -->
<div id="dropZone" class="fixed inset-0 z-[100] bg-ink-900/70 backdrop-blur-md hidden items-center justify-center pointer-events-none">
    <div class="bg-white rounded-3xl p-12 text-center shadow-2xl max-w-sm mx-4 border-2 border-dashed border-tea-400 anim-modal-in">
        <svg class="w-14 h-14 text-tea-500 mx-auto mb-4 drop-bounce" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>
        </svg>
        <h3 class="font-display text-xl font-bold text-ink-800 mb-1">释放以上传</h3>
        <p class="text-ink-400 text-sm">支持音频及视频文件</p>
    </div>
</div>

<!-- ─── Toast ─── -->
<div id="toastContainer" class="fixed bottom-24 right-4 sm:right-6 z-[60] flex flex-col gap-2 items-end pointer-events-none"></div>

<script>
const $ = id => document.getElementById(id);
const API_PREFIX = location.pathname.replace(/\/[^/]*$/, "") + '/audiom/api/vocal';

let state = {
    data: [], filtered: [], renderIndex: 0, chunkSize: 10,
    searchTimeout: null, poll: null, scrollObserver: null,
    wavesurfer: null, currentSegments: [], lastActiveIdx: -1
};

/* ── Toast ────────────────────────────────── */
function showToast(msg, type = 'info') {
    const cls = { success:'bg-tea-50 text-tea-700 border-tea-200', error:'bg-red-50 text-red-700 border-red-200', info:'bg-ink-50 text-ink-700 border-ink-200' };
    const ico = {
        success: '<svg class="w-4 h-4 text-tea-500 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 12.75 6 6 9-13.5"/></svg>',
        error: '<svg class="w-4 h-4 text-red-500 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z"/></svg>',
        info: '<svg class="w-4 h-4 text-ink-500 shrink-0" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m11.25 11.25.041-.02a.75.75 0 0 1 1.063.852l-.708 2.836a.75.75 0 0 0 1.063.853l.041-.021M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-3.75h.008v.008H12V8.25Z"/></svg>'
    };
    const el = document.createElement('div');
    el.className = `pointer-events-auto flex items-center gap-2.5 px-4 py-3 rounded-xl border shadow-lg shadow-ink-900/5 text-sm font-medium max-w-xs anim-toast-in ${cls[type]}`;
    el.innerHTML = `${ico[type]}<span>${msg}</span>`;
    $('toastContainer').appendChild(el);
    setTimeout(() => { el.classList.add('anim-toast-out'); setTimeout(() => el.remove(), 300); }, 3500);
}

/* ── Search ───────────────────────────────── */
const handleSearch = val => {
    clearTimeout(state.searchTimeout);
    state.searchTimeout = setTimeout(() => {
        const kw = val.toLowerCase().trim();
        state.filtered = state.data.filter(i => (i.title_with_tags + ' ' + (i.stt_text_preview||'')).toLowerCase().includes(kw));
        $('timeline').innerHTML = '';
        state.renderIndex = 0;
        if (state.filtered.length === 0) {
            $('emptyState').classList.remove('hidden');
            $('emptyState').querySelector('h3').textContent = kw ? '没有找到匹配的录音' : '尚无录音记录';
            $('emptyState').querySelector('p').textContent = kw ? '请尝试更换关键词' : '点击底部麦克风开始录音，或将音频/视频文件拖拽到此页面';
        } else {
            $('emptyState').classList.add('hidden');
            renderChunk();
        }
    }, 250);
};

/* ── Card Builder ─────────────────────────── */
function buildCardHTML(item) {
    const isProcessing = item.status !== 'completed' && !item.status.includes('failed');
    const isFailed = item.status.includes('failed');
    const isVideo = (item.mime_type||'').startsWith('video/');
    const hasAudio = item.audio_url && item.status !== 'uploaded';

    const badge = isProcessing
        ? `<span class="text-[11px] bg-amber-50 text-amber-600 px-2 py-0.5 rounded-md font-mono font-medium flex items-center gap-1 shrink-0"><span class="w-1.5 h-1.5 bg-amber-500 rounded-full animate-pulse"></span>处理中</span>`
        : isFailed ? `<span class="text-[11px] bg-red-50 text-red-600 px-2 py-0.5 rounded-md font-mono font-medium shrink-0">中断</span>` : '';

    const icon = isVideo
        ? `<div class="w-10 h-10 bg-violet-100 rounded-xl flex items-center justify-center shrink-0"><svg class="w-5 h-5 text-violet-500" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m15.75 10.5 4.72-4.72a.75.75 0 0 1 1.28.53v11.38a.75.75 0 0 1-1.28.53l-4.72-4.72M4.5 18.75h9a2.25 2.25 0 0 0 2.25-2.25v-9a2.25 2.25 0 0 0-2.25-2.25h-9A2.25 2.25 0 0 0 2.25 7.5v9a2.25 2.25 0 0 0 2.25 2.25Z"/></svg></div>`
        : `<div class="w-10 h-10 bg-tea-100 rounded-xl flex items-center justify-center shrink-0"><svg class="w-5 h-5 text-tea-600" fill="currentColor" viewBox="0 0 24 24"><path d="M12 14c1.66 0 3-1.34 3-3V5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3zm5-3c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z"/></svg></div>`;

    const parts = item.title_with_tags.split('#');
    const title = parts[0].trim() || '基础音频记录';
    const tags = parts.slice(1).filter(t=>t.trim()).map(t=>`<span class="text-[11px] text-tea-600 bg-tea-50 px-2 py-0.5 rounded-md border border-tea-200/60 font-medium">#${t.trim()}</span>`).join('');
    const d = item.duration || 0;
    const durStr = d > 0 ? `${Math.floor(d/60)}:${String(d%60).padStart(2,'0')}` : '';

    const player = hasAudio ? `
        <div class="flex items-center gap-2.5 mt-3 pt-3 border-t border-ink-100/60 prog-wrap" onclick="event.stopPropagation()">
            <button onclick="InlinePlayer.toggle('${item.id}','${item.audio_url}')" id="inline-btn-${item.id}"
                    class="w-9 h-9 rounded-full bg-tea-500 hover:bg-tea-400 text-white flex items-center justify-center shrink-0 transition-all shadow-sm active:scale-90">
                <svg id="inline-icon-${item.id}" class="w-4 h-4 ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            </button>
            <div class="flex-1 h-6 flex items-center cursor-pointer" id="inline-bar-${item.id}" onclick="InlinePlayer.seek(event,'${item.id}')">
                <div class="w-full prog-track h-1.5 bg-ink-200/60 rounded-full overflow-hidden transition-all duration-150">
                    <div id="inline-prog-${item.id}" class="h-full bg-tea-500 rounded-full" style="width:0%;transition:width .08s linear"></div>
                </div>
            </div>
            <span id="inline-time-${item.id}" class="text-[11px] font-mono text-ink-400 shrink-0 tabular-nums min-w-[72px] text-right">--:--</span>
        </div>` : isProcessing ? `
        <div class="flex items-center gap-2 mt-3 pt-3 border-t border-ink-100/60" onclick="event.stopPropagation()">
            <div class="w-4 h-4 border-2 border-ink-200 border-t-tea-500 rounded-full animate-spin"></div>
            <span class="text-xs text-ink-400 font-mono">转录排队中...</span>
        </div>` : '';

    const actions = `<div class="flex justify-end gap-2 mt-2 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity duration-200" onclick="event.stopPropagation()">
        ${isFailed?`<button class="flex items-center gap-1.5 text-xs text-tea-600 bg-tea-50 hover:bg-tea-100 px-3 py-1.5 rounded-lg font-semibold transition-colors" onclick="retryRecord('${item.id}')"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0 3.181 3.183a8.25 8.25 0 0 0 13.803-3.7M4.031 9.865a8.25 8.25 0 0 1 13.803-3.7l3.181 3.182"/></svg>云端重试</button>`:''}
        <button class="flex items-center gap-1.5 text-xs text-ink-400 hover:text-red-500 bg-ink-50 hover:bg-red-50 px-3 py-1.5 rounded-lg font-medium transition-colors" onclick="deleteRecord('${item.id}')"><svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="m14.74 9-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 0 1-2.244 2.077H8.084a2.25 2.25 0 0 1-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 0 0-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 0 1 3.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 0 0-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 0 0-7.5 0"/></svg>删除</button>
    </div>`;

    return `
    <div class="relative pl-12 sm:pl-16 timeline-card" id="card-${item.id}">
        <div class="absolute left-[18px] sm:left-[30px] top-5 w-3 h-3 rounded-full border-[2.5px] border-white shadow-sm z-10 ${isFailed?'bg-red-400':isProcessing?'bg-amber-400':'bg-tea-500'}"></div>
        <div class="bg-white rounded-2xl p-4 sm:p-5 shadow-sm border ${isFailed?'border-red-200/60':'border-ink-200/40'} cursor-pointer relative overflow-hidden group" onclick="DetailCtrl.open('${item.id}')">
            ${isFailed?'<div class="absolute top-0 left-0 right-0 h-0.5 bg-red-400"></div>':''}
            <div class="flex items-start gap-3.5">
                ${icon}
                <div class="flex-1 min-w-0">
                    <div class="flex items-center justify-between gap-2 mb-1">
                        <h4 class="font-semibold text-ink-800 text-[15px] sm:text-base truncate">${title}</h4>
                        ${badge}
                    </div>
                    <div class="flex items-center gap-2 mb-1.5 flex-wrap">
                        <span class="text-[11px] text-ink-400 font-mono">${item.created_at}</span>
                        ${durStr?`<span class="text-[11px] text-ink-300">·</span><span class="text-[11px] text-ink-400 font-mono">${durStr}</span>`:''}
                        ${tags}
                    </div>
                    <p class="text-sm text-ink-400 line-clamp-2 leading-relaxed">${item.stt_text_preview||(isProcessing?'云端引擎正在转录...':isFailed?'转录中断，可点击重试':'等待转录...')}</p>
                </div>
            </div>
            ${player}
            ${actions}
        </div>
    </div>`;
}

/* ── Render ───────────────────────────────── */
const renderChunk = () => {
    if (state.renderIndex >= state.filtered.length) return;
    const chunk = state.filtered.slice(state.renderIndex, state.renderIndex + state.chunkSize);
    const frag = document.createDocumentFragment();
    const tmp = document.createElement('div');
    tmp.innerHTML = chunk.map(buildCardHTML).join('');
    while (tmp.firstChild) frag.appendChild(tmp.firstChild);
    $('timeline').appendChild(frag);
    const cards = $('timeline').children;
    for (let i = state.renderIndex; i < Math.min(state.renderIndex + state.chunkSize, state.filtered.length); i++) {
        if (cards[i]) cards[i].style.animationDelay = `${(i - state.renderIndex) * 0.06}s`;
    }
    state.renderIndex += state.chunkSize;
    if (state.scrollObserver) state.scrollObserver.disconnect();
    if (state.renderIndex < state.filtered.length) {
        state.scrollObserver = new IntersectionObserver(entries => { if (entries[0].isIntersecting) renderChunk(); }, { rootMargin: '400px' });
        const ch = $('timeline').children;
        if (ch.length) state.scrollObserver.observe(ch[ch.length - 1]);
    }
};

/* ── Data ─────────────────────────────────── */
const loadData = async () => {
    try {
        if (state.data.length === 0) {
            $('timeline').innerHTML = '<div class="space-y-4">' + Array.from({length:3},()=>'<div class="relative pl-12 sm:pl-16"><div class="absolute left-[18px] sm:left-[30px] top-5 w-3 h-3 rounded-full bg-ink-200"></div><div class="bg-white rounded-2xl p-5 border border-ink-200/40"><div class="flex items-start gap-3.5"><div class="w-10 h-10 skeleton rounded-xl shrink-0"></div><div class="flex-1 space-y-2"><div class="h-3 skeleton w-24"></div><div class="h-4 skeleton w-3/4"></div><div class="h-3 skeleton w-full"></div></div></div></div></div>').join('')+'</div>';
        }
        const res = await fetch(API_PREFIX);
        state.data = await res.json();
        state.filtered = [...state.data];
        $('timeline').innerHTML = '';
        state.renderIndex = 0;
        if (state.filtered.length === 0) { $('emptyState').classList.remove('hidden'); }
        else { $('emptyState').classList.add('hidden'); renderChunk(); }
        if (state.data.some(i => i.status.startsWith('pending') || i.status.startsWith('processing'))) { if (!state.poll) state.poll = setInterval(loadData, 4000); }
        else { clearInterval(state.poll); state.poll = null; }
        if (InlinePlayer.currentId) InlinePlayer.refreshUI();
    } catch(e) { console.warn('连接中断', e); showToast('无法连接服务器', 'error'); }
};

const deleteRecord = async id => {
    if (!confirm('确定永久删除本条录音吗？')) return;
    try {
        await fetch(`${API_PREFIX}/${id}`, { method:'DELETE' });
        const card = document.getElementById(`card-${id}`);
        if (card) { card.style.transition='all .3s ease'; card.style.opacity='0'; card.style.transform='translateX(20px)'; setTimeout(loadData,300); } else loadData();
        showToast('已删除','info');
    } catch(e) { showToast('删除失败','error'); }
};

const retryRecord = async id => {
    try {
        await fetch(`${API_PREFIX}/retry`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id}) });
        showToast('已提交云端重试','success'); loadData();
    } catch(e) { showToast('重试失败','error'); }
};

/* ── Inline Player ────────────────────────── */
const InlinePlayer = {
    audio: new Audio(),
    currentId: null,

    init() {
        this.audio.addEventListener('ended', () => this.onEnded());
        this.audio.addEventListener('pause', () => this.syncIcon());
        this.audio.addEventListener('play', () => this.syncIcon());
        this.audio.addEventListener('timeupdate', () => this.updateProgress());
        this.audio.addEventListener('loadedmetadata', () => this.updateProgress());
    },

    toggle(id, url) {
        if (!url) return;
        if (this.currentId === id) {
            this.audio.paused ? this.audio.play() : this.audio.pause();
        } else {
            this.resetCard(this.currentId);
            this.currentId = id;
            this.audio.src = url;
            this.audio.play().catch(()=>{});
        }
    },

    seek(event, id) {
        if (this.currentId !== id || !this.audio.duration) return;
        const bar = document.getElementById('inline-bar-' + id);
        if (!bar) return;
        const rect = bar.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
        this.audio.currentTime = ratio * this.audio.duration;
    },

    onEnded() {
        this.resetCard(this.currentId);
        this.currentId = null;
    },

    syncIcon() {
        if (!this.currentId) return;
        const playing = !this.audio.paused;
        const icon = document.getElementById('inline-icon-' + this.currentId);
        if (icon) icon.innerHTML = playing
            ? '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'
            : '<path d="M8 5v14l11-7z"/>';
    },

    updateProgress() {
        if (!this.currentId) return;
        const prog = document.getElementById('inline-prog-' + this.currentId);
        const time = document.getElementById('inline-time-' + this.currentId);
        const dur = this.audio.duration || 0;
        const cur = this.audio.currentTime || 0;
        if (prog && dur) prog.style.width = `${(cur/dur)*100}%`;
        if (time) {
            const fmt = s => `${Math.floor(s/60)}:${String(Math.floor(s%60)).padStart(2,'0')}`;
            time.textContent = dur ? `${fmt(cur)} / ${fmt(dur)}` : '--:--';
        }
    },

    resetCard(id) {
        if (!id) return;
        const icon = document.getElementById('inline-icon-' + id);
        const prog = document.getElementById('inline-prog-' + id);
        const time = document.getElementById('inline-time-' + id);
        if (icon) icon.innerHTML = '<path d="M8 5v14l11-7z"/>';
        if (prog) prog.style.width = '0%';
        if (time) time.textContent = '--:--';
    },

    refreshUI() {
        if (!this.currentId) return;
        this.syncIcon();
        this.updateProgress();
    }
};
InlinePlayer.init();

/* ── Record Controller (支持首页FAB录音) ────────────────────── */
const RecordCtrl = {
    mediaRecorder: null, audioChunks: [], startTime: 0, timer: null, isRecording: false,

    open() {
        $('recordModal').classList.remove('hidden');
        document.querySelectorAll('#recordModal input[type=file]').forEach(i => i.value = '');
    },

    close() {
        $('recordModal').classList.add('hidden');
    },

    async toggleRecord() {
        if (!this.isRecording) {
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                let opts = {};
                for (const t of ['audio/webm','audio/mp4','audio/ogg','audio/aac']) {
                    if (typeof MediaRecorder.isTypeSupported === 'function' && MediaRecorder.isTypeSupported(t)) { opts.mimeType = t; break; }
                }
                this.mediaRecorder = new MediaRecorder(stream, opts);
                this.audioChunks = [];
                this.mediaRecorder.ondataavailable = e => { if (e.data?.size > 0) this.audioChunks.push(e.data); };
                this.mediaRecorder.onstop = () => {
                    const mime = this.mediaRecorder.mimeType || opts.mimeType || 'audio/webm';
                    const blob = new Blob(this.audioChunks, { type: mime });
                    let ext = '.webm';
                    if (mime.includes('mp4')||mime.includes('m4a')) ext='.m4a'; else if (mime.includes('ogg')) ext='.ogg'; else if (mime.includes('aac')) ext='.aac'; else if (mime.includes('wav')) ext='.wav';
                    const dur = Math.round((Date.now() - this.startTime) / 1000);
                    if (blob.size === 0 || dur < 1) { 
                        showToast('录音时间过短','error'); 
                        this._resetFAB();
                        return; 
                    }
                    this.handleUpload(new File([blob], `Rec_${Date.now()}${ext}`, { type: mime }), dur, true);
                };
                this.mediaRecorder.start(1000);
                this.isRecording = true;
                this.startTime = Date.now();
                
                // 触发首页 FAB 录音动效
                $('quickRecBtn').className = 'w-16 h-16 rounded-full bg-red-500 text-white flex items-center justify-center pointer-events-auto transition-all duration-300 active:scale-90 border-[3px] border-white shadow-xl recording-pulse relative';
                $('quickRecDot').classList.remove('hidden');
                $('quickRecDot').classList.add('animate-pulse');
                $('quickRecTime').classList.remove('hidden');
                
                this.timer = setInterval(() => {
                    const s = Math.floor((Date.now() - this.startTime) / 1000);
                    $('quickRecTime').innerText = `🔴 ${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;
                }, 500);
            } catch(e) { showToast('无法访问麦克风，请检查系统权限设置','error'); }
        } else { 
            this.stopRecord(); 
        }
    },

    stopRecord() {
        if(this.mediaRecorder && this.isRecording) {
            this.mediaRecorder.stop();
            this.mediaRecorder.stream.getTracks().forEach(t => t.stop());
        }
        this.isRecording = false;
        clearInterval(this.timer);
        this._resetFAB();
    },
    
    _resetFAB() {
        $('quickRecBtn').className = 'w-16 h-16 rounded-full bg-ink-900 text-white flex items-center justify-center pointer-events-auto transition-all duration-300 active:scale-90 border-[3px] border-white shadow-xl hover:bg-ink-800 relative group';
        $('quickRecDot').classList.add('hidden');
        $('quickRecTime').classList.add('hidden');
        $('quickRecTime').innerText = '00:00';
    },

    async handleUpload(file, explicitDuration, isRecorded = false) {
        if (!file) return;
        const isVideo = file.type.startsWith('video/');
        $('recordModal').classList.add('hidden');
        
        // 使用全局透明遮罩防止多次点击
        $('uploadOverlay').classList.remove('hidden');
        $('uploadStatusText').innerText = isVideo ? '正在提取视频音轨...' : '正在封包并上传...';

        let duration = explicitDuration || 0;
        if (!duration) {
            duration = await new Promise(resolve => {
                const url = URL.createObjectURL(file);
                const tmp = isVideo ? document.createElement('video') : new Audio();
                tmp.preload = 'metadata';
                const t = setTimeout(() => { resolve(0); URL.revokeObjectURL(url); }, 3000);
                tmp.onloadedmetadata = () => { clearTimeout(t); resolve(Math.round(tmp.duration||0)); URL.revokeObjectURL(url); };
                tmp.onerror = () => { clearTimeout(t); resolve(0); URL.revokeObjectURL(url); };
                tmp.src = url;
            });
        }

        const fd = new FormData();
        fd.append('file', file);
        fd.append('duration', duration);

        try {
            const res = await fetch(`${API_PREFIX}/upload`, { method:'POST', body: fd });
            if (!res.ok) throw new Error('HTTP ' + res.status);
            const data = await res.json();
            if (data.id) fetch(`${API_PREFIX}/start`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:[data.id]}) });

            const rec = {
                id: data.id || ('tmp_'+Date.now()),
                title_with_tags: file.name.replace(/\.[^.]+$/, ''),
                stt_text_preview: '', status: 'pending',
                created_at: new Date().toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(/\//g,'-'),
                duration, mime_type: file.type, sync_data: [], audio_url: data.audio_url||''
            };
            state.data.unshift(rec);
            state.filtered = [...state.data];
            $('emptyState').classList.add('hidden');
            const tmp = document.createElement('div');
            tmp.innerHTML = buildCardHTML(rec);
            const card = tmp.firstElementChild;
            card.classList.add('card-enter');
            const tl = $('timeline');
            tl.firstChild ? tl.insertBefore(card, tl.firstChild) : tl.appendChild(card);

            $('uploadOverlay').classList.add('hidden');
            showToast(isVideo ? '视频提取成功，正发往云端...' : '录音上传成功，准备转录...', 'success');
            if (!state.poll) state.poll = setInterval(loadData, 4000);
        } catch(e) {
            $('uploadOverlay').classList.add('hidden');
            if (isRecorded) {
                const a = document.createElement('a');
                a.href = URL.createObjectURL(file); a.download = file.name;
                document.body.appendChild(a); a.click(); a.remove();
                showToast('上传失败，已保存至本地下载目录','error');
            } else { showToast('上传失败，请检查网络连接','error'); }
        }
    }
};

/* ── Detail Controller ────────────────────── */
const DetailCtrl = {
    open(id) {
        const item = state.data.find(i => i.id === id);
        if (!item) return;
        $('modalTitle').innerText = item.title_with_tags.split('#')[0].trim();
        state.currentSegments = item.sync_data || [];
        state.lastActiveIdx = -1;
        const c = $('syncTextContainer');
        if (item.status.includes('failed')) {
            c.innerHTML = `<div class="bg-red-50 border border-red-200/60 rounded-xl p-5 text-center"><svg class="w-8 h-8 text-red-400 mx-auto mb-3" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m9-.75a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 3.75h.008v.008H12v-.008Z"/></svg><p class="font-semibold text-red-700 mb-1">转录中断</p><p class="text-sm text-red-500/80">音频已安全保存，请返回列表点击「重试」</p></div>`;
        } else if (state.currentSegments.length > 0) {
            c.innerHTML = state.currentSegments.map((s,idx) => `<span class="sync-text" id="sync-text-${idx}" data-start="${s.start}" onclick="DetailCtrl.seek(${s.start})">${s.text}</span>`).join('');
        } else if (item.stt_text_preview) {
            c.innerHTML = `<p class="text-ink-600 whitespace-pre-wrap leading-relaxed">${item.stt_text_preview}</p>`;
        } else {
            c.innerHTML = `<div class="text-center py-10"><div class="w-8 h-8 border-2 border-ink-200 border-t-tea-500 rounded-full animate-spin mx-auto mb-4"></div><p class="text-ink-400 text-sm font-mono">正在等待云端引擎转录...</p></div>`;
        }
        $('detailModal').classList.remove('hidden');
        $('progressFill').style.width = '0%';
        this.initWavesurfer(item.audio_url);
    },

    close() {
        if (state.wavesurfer) { state.wavesurfer.destroy(); state.wavesurfer = null; }
        $('detailModal').classList.add('hidden');
    },

    togglePlay() { if (state.wavesurfer) state.wavesurfer.playPause(); },

    seek(time) {
        if (!state.wavesurfer) return;
        const dur = state.wavesurfer.getDuration();
        if (dur > 0) { state.wavesurfer.seekTo(time / dur); state.wavesurfer.play(); }
    },

    initWavesurfer(url) {
        if (state.wavesurfer) state.wavesurfer.destroy();
        const fmt = s => `${String(Math.floor(s/60)).padStart(2,'0')}:${String(Math.floor(s%60)).padStart(2,'0')}`;
        state.wavesurfer = WaveSurfer.create({
            container:'#waveform', waveColor:'#506572', progressColor:'#14b8a6',
            cursorColor:'#2dd4bf', cursorWidth:2, barWidth:2, barGap:1, barRadius:2,
            height:48, url, autoplay:true, dragToSeek:true
        });
        state.wavesurfer.on('ready', () => { $('timeDisplay').innerText = `00:00 / ${fmt(state.wavesurfer.getDuration())}`; });
        state.wavesurfer.on('timeupdate', t => {
            const dur = state.wavesurfer.getDuration();
            $('timeDisplay').innerText = `${fmt(t)} / ${fmt(dur)}`;
            if (dur > 0) $('progressFill').style.width = `${(t/dur)*100}%`;
            let ai = -1;
            for (let i = 0; i < state.currentSegments.length; i++) {
                if (t >= state.currentSegments[i].start && (t <= state.currentSegments[i].end || !state.currentSegments[i].end)) { ai = i; break; }
            }
            if (ai !== state.lastActiveIdx) {
                document.querySelectorAll('.sync-text').forEach((el,idx) => el.classList.toggle('active', idx === ai));
                if (ai !== -1) {
                    const a = document.getElementById('sync-text-'+ai);
                    if (a && state.wavesurfer.isPlaying()) a.scrollIntoView({behavior:'smooth',block:'center'});
                }
                state.lastActiveIdx = ai;
            }
        });
        state.wavesurfer.on('play', () => { $('playIcon').innerHTML = '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>'; });
        state.wavesurfer.on('pause', () => { $('playIcon').innerHTML = '<path d="M8 5v14l11-7z"/>'; });
        $('progressBar').onclick = e => {
            if (!state.wavesurfer) return;
            const r = e.currentTarget.getBoundingClientRect();
            state.wavesurfer.seekTo(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)));
        };
    }
};

/* ── Drag & Drop ──────────────────────────── */
(function initDragDrop() {
    let counter = 0;
    const dz = $('dropZone');

    document.addEventListener('dragenter', e => {
        e.preventDefault();
        if (e.dataTransfer?.types?.includes('Files')) {
            counter++;
            if (counter === 1) dz.classList.remove('hidden'), dz.classList.add('flex');
        }
    });
    document.addEventListener('dragleave', e => {
        e.preventDefault();
        counter--;
        if (counter <= 0) { counter = 0; dz.classList.add('hidden'); dz.classList.remove('flex'); }
    });
    document.addEventListener('dragover', e => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; });
    document.addEventListener('drop', e => {
        e.preventDefault();
        counter = 0;
        dz.classList.add('hidden'); dz.classList.remove('flex');
        const files = e.dataTransfer?.files;
        if (!files || !files.length) return;
        const file = files[0];
        if (file.type.startsWith('audio/') || file.type.startsWith('video/')) {
            RecordCtrl.handleUpload(file, 0, false);
        } else {
            showToast('不支持的文件类型，请上传音频或视频文件','error');
        }
    });
})();

/* ── Init ─────────────────────────────────── */
loadData();
</script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'snippets': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>代码片段管理</title>
<script src="static/cdn/marked.min.js"></script>
    <link href="static/cdn/atom-one-dark.min.css" rel="stylesheet">
    <script src="static/cdn/highlight.min.js"></script>
    <style> 
        :root { --primary: #007bff; --border: #ddd; --bg: #f5f7fa; }
        body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; height: 100vh; overflow: hidden; }
        
        /* 左侧边栏 */
        .sidebar { width: 300px; background: #fff; border-right: 1px solid var(--border); display: flex; flex-direction: column; }
        .sidebar-header { padding: 15px; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: 10px; }
        .search-box { padding: 8px; border: 1px solid var(--border); border-radius: 4px; outline: none; }
        .btn-new { background: var(--primary); color: white; border: none; padding: 8px; border-radius: 4px; cursor: pointer; }
        .btn-new:hover { background: #0056b3; }
        .snippet-list { list-style: none; padding: 0; margin: 0; overflow-y: auto; flex: 1; }
        .snippet-item { padding: 12px 15px; border-bottom: 1px solid #eee; cursor: pointer; display: flex; flex-direction: column; }
        .snippet-item:hover { background: var(--bg); }
        .snippet-item.active { background: #e6f2ff; border-left: 3px solid var(--primary); }
        .item-title { font-weight: 500; color: #333; margin-bottom: 4px; }
        .item-time { font-size: 12px; color: #888; }

        /* 右侧主内容区 */
        .main-content { flex: 1; background: var(--bg); display: flex; flex-direction: column; overflow-y: auto; }
        .panel { padding: 30px; max-width: 900px; margin: 0 auto; width: 100%; box-sizing: border-box; }
        .hidden { display: none !important; }

        /* 查看模式 */
        .toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        .toolbar h2 { margin: 0; color: #333; }
        .action-btns button { margin-left: 10px; padding: 6px 15px; border: 1px solid var(--border); background: white; border-radius: 4px; cursor: pointer; }
        .action-btns button.primary { background: var(--primary); color: white; border-color: var(--primary); }
        .action-btns button.danger { color: #dc3545; border-color: #dc3545; }
        .action-btns button:hover { opacity: 0.8; }
        
        .markdown-body pre { background: #282c34; padding: 15px; border-radius: 6px; overflow-x: auto; margin-top: 10px; margin-bottom: 10px; }
        .markdown-body code { font-family: Consolas, Monaco, 'Andale Mono', 'Ubuntu Mono', monospace; font-size: 14px; }
        .markdown-body p code { background: #eee; color: #d63384; padding: 2px 4px; border-radius: 3px; }

        /* 编辑模式 */
        .edit-title-input { width: 100%; padding: 10px; font-size: 18px; margin-bottom: 15px; border: 1px solid var(--border); border-radius: 4px; box-sizing: border-box; }
        .edit-label { font-size: 14px; font-weight: bold; color: #555; margin-bottom: 5px; display: block; }
        .edit-notes-textarea { width: 100%; height: 10vh; padding: 10px; font-family: sans-serif; font-size: 14px; border: 1px solid var(--border); border-radius: 4px; resize: vertical; box-sizing: border-box; margin-bottom: 15px; }
        .edit-code-textarea { width: 100%; height: 45vh; padding: 10px; font-family: monospace; font-size: 14px; border: 1px solid var(--border); border-radius: 4px; resize: vertical; box-sizing: border-box; line-height: 1.5; background: #fafafa; }
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

    <div class="sidebar">
        <div class="sidebar-header">
            <button class="btn-new" onclick="showEditPanel()">+ 新建片段</button>
            <input type="text" id="searchInput" class="search-box" placeholder="搜索标题..." onkeyup="filterList()">
        </div>
        <ul class="snippet-list" id="snippetList"></ul>
    </div>

    <div class="main-content">
        <div id="emptyPanel" class="panel" style="text-align: center; color: #888; margin-top: 20%;">
            请在左侧选择一个代码片段，或点击新建。
        </div>

        <div id="viewPanel" class="panel hidden">
            <div class="toolbar">
                <h2 id="viewTitle"></h2>
                <div class="action-btns">
                    <button class="primary" onclick="copyCode(this)">一键复制</button>
                    <button onclick="editCurrent()">编辑</button>
                    <button class="danger" onclick="deleteCurrent()">删除</button>
                </div>
            </div>
            <div id="viewContent" class="markdown-body"></div>
        </div>

        <div id="editPanel" class="panel hidden">
            <div class="toolbar">
                <h2 id="editModeTitle">新建片段</h2>
                <div class="action-btns">
                    <button class="primary" onclick="saveSnippet()">保存</button>
                    <button onclick="cancelEdit()">取消</button>
                </div>
            </div>
            <input type="hidden" id="editId">
            <input type="text" id="editTitle" class="edit-title-input" placeholder="输入标题 (如: Python Flask 基础模板)">
            
            <label class="edit-label">📝 描述/笔记 (选填，支持 Markdown)</label>
            <textarea id="editNotes" class="edit-notes-textarea" placeholder="这里可以写一些说明文字..."></textarea>
            
            <label class="edit-label">💻 纯代码区 (直接粘贴，无需写 ``` 符号)</label>
            <textarea id="editCode" class="edit-code-textarea" placeholder="直接粘贴纯代码..."></textarea>
        </div>
    </div>

    <script>
        let currentSnippetRaw = null; 

        document.addEventListener('DOMContentLoaded', loadList);

        async function loadList(activeId = null) {
            try {
                const res = await fetch(API_BASE + '/api/snippets/list');
                const data = await res.json();
                const listEl = document.getElementById('snippetList');
                listEl.innerHTML = '';
                
                data.snippets.forEach(s => {
                    const li = document.createElement('li');
                    li.className = `snippet-item ${s.id === activeId ? 'active' : ''}`;
                    li.setAttribute('data-title', s.title.toLowerCase());
                    li.onclick = () => loadSnippet(s.id, li);
                    
                    const timeStr = new Date(s.created_at).toLocaleString('zh-CN', { hour12: false });
                    
                    li.innerHTML = `
                        <div class="item-title">${s.title}</div>
                        <div class="item-time">${timeStr}</div>
                    `;
                    listEl.appendChild(li);
                });
            } catch (err) {
                console.error('加载列表失败:', err);
            }
        }

        function filterList() {
            const keyword = document.getElementById('searchInput').value.toLowerCase();
            const items = document.querySelectorAll('.snippet-item');
            items.forEach(item => {
                const title = item.getAttribute('data-title');
                item.style.display = title.includes(keyword) ? 'flex' : 'none';
            });
        }

        async function loadSnippet(id, liElement) {
            document.querySelectorAll('.snippet-item').forEach(el => el.classList.remove('active'));
            if (liElement) liElement.classList.add('active');

            try {
                const res = await fetch(API_BASE + `/api/snippets/raw/${id}`);
                const data = await res.json();
                if (res.ok) {
                    currentSnippetRaw = data;
                    document.getElementById('viewTitle').innerText = data.title;
                    
                    document.getElementById('viewContent').innerHTML = marked.parse(data.content, {
                        gfm: true,
                        breaks: true
                    });
                    
                    document.querySelectorAll('#viewContent pre code').forEach((block) => {
                        hljs.highlightElement(block);
                    });

                    switchPanel('view');
                }
            } catch (err) {
                console.error('加载详情失败:', err);
            }
        }

        function showEditPanel() {
            document.querySelectorAll('.snippet-item').forEach(el => el.classList.remove('active'));
            document.getElementById('editModeTitle').innerText = '新建片段';
            document.getElementById('editId').value = '';
            document.getElementById('editTitle').value = '';
            document.getElementById('editNotes').value = '';
            document.getElementById('editCode').value = '';
            switchPanel('edit');
        }

        function editCurrent() {
            if (!currentSnippetRaw) return;
            document.getElementById('editModeTitle').innerText = '编辑片段';
            document.getElementById('editId').value = currentSnippetRaw.id;
            document.getElementById('editTitle').value = currentSnippetRaw.title;
            
            // 使用正则分离代码和笔记
            const rawContent = currentSnippetRaw.content;
            const codeMatch = rawContent.match(/```[a-zA-Z]*\n([\s\S]*?)\n```/);
            
            if (codeMatch) {
                // 提取出了代码块
                document.getElementById('editCode').value = codeMatch[1].trim();
                // 将代码块从原文中移除，剩下的就是笔记
                document.getElementById('editNotes').value = rawContent.replace(codeMatch[0], '').trim();
            } else {
                // 兼容老数据：如果没有代码块标记，默认全是代码
                document.getElementById('editCode').value = rawContent.trim();
                document.getElementById('editNotes').value = '';
            }
            
            switchPanel('edit');
        }

        async function saveSnippet() {
            const id = document.getElementById('editId').value;
            const title = document.getElementById('editTitle').value.trim();
            const notes = document.getElementById('editNotes').value.trim();
            const code = document.getElementById('editCode').value.trim();

            if (!title) return alert('标题不能为空');
            if (!code && !notes) return alert('内容不能为空');

            // 自动组合为带有代码块的 Markdown
            let finalContent = '';
            if (notes) {
                finalContent += notes + '\n\n';
            }
            if (code) {
                finalContent += '```\n' + code + '\n```';
            }

            const payload = { title, content: finalContent };
            if (id) payload.id = parseInt(id);

            try {
                const res = await fetch(API_BASE + '/api/snippets/save', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                
                if (res.ok) {
                    await loadList();
                    if (id) {
                        loadSnippet(id);
                    } else {
                        switchPanel('empty');
                    }
                } else {
                    const err = await res.json();
                    alert('保存失败: ' + err.error);
                }
            } catch (err) {
                alert('网络错误');
            }
        }

        async function deleteCurrent() {
            if (!currentSnippetRaw || !confirm('确定要删除此片段吗？')) return;
            
            try {
                const res = await fetch(API_BASE + `/api/snippets/delete/${currentSnippetRaw.id}`, { method: 'DELETE' });
                if (res.ok) {
                    currentSnippetRaw = null;
                    await loadList();
                    switchPanel('empty');
                }
            } catch (err) {
                alert('删除失败');
            }
        }

        function cancelEdit() {
            if (currentSnippetRaw) {
                switchPanel('view');
            } else {
                switchPanel('empty');
            }
        }

        function switchPanel(mode) {
            document.getElementById('emptyPanel').classList.add('hidden');
            document.getElementById('viewPanel').classList.add('hidden');
            document.getElementById('editPanel').classList.add('hidden');

            if (mode === 'empty') document.getElementById('emptyPanel').classList.remove('hidden');
            if (mode === 'view') document.getElementById('viewPanel').classList.remove('hidden');
            if (mode === 'edit') document.getElementById('editPanel').classList.remove('hidden');
        }

        function copyCode(btn) {
            const container = document.getElementById('viewContent');
            const codeBlocks = container.querySelectorAll('code');
            let textToCopy = '';
            
            if (codeBlocks.length > 0) {
                textToCopy = Array.from(codeBlocks).map(cb => cb.innerText).join('\n\n');
            } else {
                textToCopy = container.innerText;
            }

            navigator.clipboard.writeText(textToCopy).then(() => {
                const originalText = btn.innerText;
                btn.innerText = '已复制!';
                btn.style.background = '#28a745';
                btn.style.borderColor = '#28a745';
                setTimeout(() => {
                    btn.innerText = originalText;
                    btn.style.background = 'var(--primary)';
                    btn.style.borderColor = 'var(--primary)';
                }, 2000);
            });
        }
    </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'product': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>数据追踪看板 (Vue+Tailwind)</title>
    <script src="static/cdn/tailwindcss3.4.17.js"></script>
    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <script src="static/cdn/compressor.min.js"></script>

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
<body class="bg-gray-100 text-gray-800 p-4 md:p-6 font-sans">
    <div id="app" class="mx-auto" style="max-width: 800px;" v-cloak>
        <header class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
            <h2 class="text-2xl font-bold flex items-center gap-2">📈 数据变化追踪</h2>
            <div class="flex gap-2">
                <button @click="exportCSV" class="px-4 py-2 bg-gray-200 hover:bg-gray-300 rounded shadow-sm text-sm">导出 CSV</button>
                <button @click="$refs.fileInput.click()" class="px-4 py-2 bg-gray-200 hover:bg-gray-300 rounded shadow-sm text-sm">导入 CSV</button>
                <input type="file" ref="fileInput" accept=".csv" class="hidden" @change="importCSV">
                <button @click="openItemModal()" class="px-4 py-2 bg-blue-500 hover:bg-blue-600 text-white rounded shadow-sm text-sm">+ 新建追踪项</button>
            </div>
        </header>

        <div v-if="items.length === 0" class="text-center text-gray-500 py-10">暂无数据追踪项目，请点击右上角新建。</div>
        
        <div class="grid gap-[15px]" style="grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));">
            <div v-for="item in items" :key="item.id" class="bg-white rounded-xl shadow p-5 flex flex-col gap-4">
                <div class="flex items-center gap-3">
                    <img v-if="item.image" :src="item.image" class="w-12 h-12 rounded-lg object-cover bg-gray-100 shrink-0">
                    <div v-else class="w-12 h-12 rounded-lg bg-gray-100 flex items-center justify-center text-2xl shrink-0">{{ guessEmoji(item.name) }}</div>
                    <div class="flex-grow">
                        <h3 class="font-bold text-lg">{{ item.name }}</h3>
                        <span class="text-xs bg-gray-100 text-gray-500 px-2 py-1 rounded">参数: {{ item.params.map(p => p.name).join(', ') }}</span>
                    </div>
                </div>

                <div class="flex-grow overflow-auto max-h-[220px]">
                    <table v-if="item.history && item.history.length" class="w-full text-sm text-left relative">
                        <thead class="text-gray-400 border-b sticky top-0 bg-white z-10 shadow-sm">
                            <tr>
                                <th class="py-2 font-normal">时间</th>
                                <th v-for="p in item.params" :key="p.id" class="py-2 font-normal">{{ p.name }}</th>
                            </tr>
                        </thead>
                        <tbody>
                            <template v-for="displayData in [getDisplayRecords(item)]" :key="item.id">
                                <tr v-for="(record, index) in displayData" :key="record.date" class="border-b last:border-0 hover:bg-gray-50 transition-colors">
                                    <td class="py-2 text-gray-500 whitespace-nowrap">
                                        {{ formatDate(record.date) }}
                                        <span v-if="record._merged" class="text-[10px] text-blue-500 bg-blue-50 rounded px-1 ml-0.5" title="互补记录已合并 (明细见CSV)">合</span>
                                    </td>
                                    <td v-for="p in item.params" :key="p.id" class="py-2">
                                        {{ record.values[p.id] !== undefined && record.values[p.id] !== '' ? record.values[p.id] : '-' }}
                                        <span v-html="getTrendIcon(index, p.id, displayData)"></span>
                                    </td>
                                </tr>
                            </template>
                        </tbody>
                    </table>
                    <div v-else class="text-sm text-gray-400 py-2">暂无数据记录</div>
                </div>

                <div class="flex justify-end gap-2 pt-3 border-t border-gray-100 mt-auto">
                    <button @click="openRecordModal(item)" class="px-3 py-1.5 bg-green-500 hover:bg-green-600 text-white rounded text-xs">➕ 录入</button>
                    <button @click="openItemModal(item)" class="px-3 py-1.5 bg-gray-100 hover:bg-gray-200 rounded text-xs">编辑/排序</button>
                    <button @click="deleteItem(item.id)" class="px-3 py-1.5 bg-red-100 hover:bg-red-200 text-red-600 rounded text-xs">删除</button>
                </div>
            </div>
        </div>

        <div v-if="modals.item || modals.record" class="fixed inset-0 bg-black bg-opacity-50 flex justify-center items-center z-50 p-4">
            
            <div v-if="modals.item" class="bg-white rounded-xl p-6 w-full max-w-md shadow-xl max-h-[90vh] overflow-y-auto">
                <h3 class="text-lg font-bold mb-4">{{ itemForm.id ? '编辑追踪项目' : '新建追踪项目' }}</h3>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium mb-1">物品图片 (可选)</label>
                        <div class="flex items-center gap-3">
                            <button @click="$refs.imageInput.click()" class="px-3 py-2 bg-gray-100 border border-dashed border-gray-300 rounded text-sm">📷 拍照 / 选图</button>
                            <input type="file" ref="imageInput" accept="image/*" class="hidden" @change="handleImage">
                            <img v-if="itemForm.image" :src="itemForm.image" class="w-10 h-10 rounded object-cover border">
                        </div>
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1">项目名称</label>
                        <input v-model="itemForm.name" type="text" placeholder="如: 某品牌锂电池" class="w-full p-2 border rounded focus:ring-2 focus:ring-blue-200 outline-none">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-2">追踪参数列表 (按此顺序显示)</label>
                        <div v-for="(p, idx) in itemForm.params" :key="p.id" class="flex items-center gap-2 mb-2">
                            <input v-model="p.name" type="text" placeholder="参数名" class="flex-grow p-1.5 border rounded focus:ring-2 focus:ring-blue-200 outline-none text-sm">
                            <button @click="moveParam(idx, -1)" :disabled="idx === 0" class="p-1.5 bg-gray-100 rounded hover:bg-gray-200 disabled:opacity-30 text-sm">↑</button>
                            <button @click="moveParam(idx, 1)" :disabled="idx === itemForm.params.length - 1" class="p-1.5 bg-gray-100 rounded hover:bg-gray-200 disabled:opacity-30 text-sm">↓</button>
                            <button @click="removeParam(idx)" class="p-1.5 bg-red-50 text-red-500 rounded hover:bg-red-100 text-sm">✖</button>
                        </div>
                        <button @click="addParam" class="w-full py-2 mt-2 border border-dashed border-gray-300 rounded text-gray-500 text-sm hover:bg-gray-50">+ 添加新参数</button>
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6">
                    <button @click="modals.item = false" class="px-4 py-2 bg-gray-100 rounded text-sm">取消</button>
                    <button @click="saveItem" class="px-4 py-2 bg-blue-500 text-white rounded text-sm">保存</button>
                </div>
            </div>

            <div v-if="modals.record" class="bg-white rounded-xl p-6 w-full max-w-md shadow-xl max-h-[90vh] overflow-y-auto">
                <h3 class="text-lg font-bold mb-4">录入数据 - {{ currentTargetItem.name }}</h3>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium mb-1">记录时间</label>
                        <input v-model="recordForm.timeInput" type="datetime-local" class="w-full p-2 border rounded outline-none">
                    </div>
                    <div v-for="p in currentTargetItem.params" :key="p.id">
                        <label class="block text-sm font-medium mb-1">{{ p.name }}</label>
                        <input v-model.number="recordForm.values[p.id]" type="number" step="any" placeholder="输入数值" class="w-full p-2 border rounded focus:ring-2 focus:ring-green-200 outline-none">
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6">
                    <button @click="modals.record = false" class="px-4 py-2 bg-gray-100 rounded text-sm">取消</button>
                    <button @click="saveRecord" class="px-4 py-2 bg-green-500 text-white rounded text-sm">保存记录</button>
                </div>
            </div>

        </div>
    </div>

    <script>
        const { createApp, ref, reactive, onMounted } = Vue;

        createApp({
            setup() {
                const API_URL = API_BASE + '/api2/data_tracking';
                const items = ref([]);
                const modals = reactive({ item: false, record: false });
                const currentTargetItem = ref(null);
                const itemForm = reactive({ id: '', name: '', params: [], image: '' });
                const recordForm = reactive({ timeInput: '', exactTime: null, values: {} });

                // 生成唯一ID的辅助函数
                const generateId = (prefix) => prefix + '_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5);

                // 数据结构迁移：将旧的字符串数组转为对象数组，保留历史数据映射
                const migrateOldData = (dataList) => {
                    dataList.forEach(item => {
                        if (item.params && item.params.length > 0 && typeof item.params[0] === 'string') {
                            const newParams = [];
                            const paramMap = {}; // 记录 { "旧名称": "新ID" }
                            item.params.forEach(pName => {
                                const newId = generateId('p');
                                newParams.push({ id: newId, name: pName });
                                paramMap[pName] = newId;
                            });
                            item.params = newParams;
                            
                            // 迁移历史记录的 key
                            if (item.history) {
                                item.history.forEach(record => {
                                    const newValues = {};
                                    for (const [oldName, val] of Object.entries(record.values)) {
                                        if (paramMap[oldName]) newValues[paramMap[oldName]] = val;
                                    }
                                    record.values = newValues;
                                });
                            }
                        }
                    });
                };

                const loadData = async () => {
                    try {
                        const res = await fetch(API_URL);
                        if (res.status === 403) return window.location.href = API_BASE + '/';
                        const data = await res.json();
                        const fetchedItems = data[0]?.items || [];
                        migrateOldData(fetchedItems);
                        items.value = fetchedItems;
                    } catch (err) {
                        items.value = [];
                    }
                };

                const syncData = async () => {
                    try {
                        await fetch(API_URL, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ items: items.value })
                        });
                    } catch (e) { console.error("同步失败", e); }
                };

                // --- 项目参数编辑逻辑 ---
                const openItemModal = (item = null) => {
                    if (item) {
                        itemForm.id = item.id;
                        itemForm.name = item.name;
                        itemForm.image = item.image;
                        // 深度克隆，防止未保存前直接污染页面
                        itemForm.params = JSON.parse(JSON.stringify(item.params));
                    } else {
                        Object.assign(itemForm, { id: '', name: '', params: [], image: '' });
                    }
                    modals.item = true;
                };

                const addParam = () => {
                    itemForm.params.push({ id: generateId('p'), name: '' });
                };

                const removeParam = (index) => {
                    itemForm.params.splice(index, 1);
                };

                const moveParam = (index, dir) => {
                    if (index + dir < 0 || index + dir >= itemForm.params.length) return;
                    const temp = itemForm.params[index];
                    itemForm.params[index] = itemForm.params[index + dir];
                    itemForm.params[index + dir] = temp;
                };

                const saveItem = () => {
                    const validParams = itemForm.params.filter(p => p.name.trim() !== '');
                    if (!itemForm.name || validParams.length === 0) return alert("名称不可为空，且至少需保留一个命名的参数！");

                    if (itemForm.id) {
                        const target = items.value.find(i => i.id === itemForm.id);
                        Object.assign(target, { name: itemForm.name, params: validParams, image: itemForm.image });
                    } else {
                        items.value.push({
                            id: generateId('item'), name: itemForm.name, params: validParams, image: itemForm.image, history: []
                        });
                    }
                    modals.item = false;
                    syncData();
                };

                const deleteItem = (id) => {
                    if (confirm("确认删除该项目及所有数据？")) {
                        items.value = items.value.filter(i => i.id !== id);
                        syncData();
                    }
                };

                // --- 录入数据逻辑 ---
                const openRecordModal = (item) => {
                    currentTargetItem.value = item;
                    const now = new Date();
                    const localNow = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
                    
                    recordForm.timeInput = localNow.toISOString().slice(0, 16);
                    recordForm.exactTime = now;
                    recordForm.values = {};
                    modals.record = true;
                };

                const saveRecord = () => {
                    if (!recordForm.timeInput || Object.keys(recordForm.values).length === 0) return alert("请至少录入一项有效数据！");
                    
                    const localNowRef = new Date(recordForm.exactTime.getTime() - recordForm.exactTime.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
                    
                    let finalDateStr;
                    if (recordForm.timeInput === localNowRef) {
                        finalDateStr = recordForm.exactTime.toISOString();
                    } else {
                        finalDateStr = new Date(recordForm.timeInput).toISOString();
                    }

                    if (!currentTargetItem.value.history) currentTargetItem.value.history = [];
                    currentTargetItem.value.history.push({
                        date: finalDateStr,
                        values: { ...recordForm.values } // 这里存的 key 是 param.id
                    });
                    modals.record = false;
                    syncData();
                };

                const handleImage = (e) => {
                    const file = e.target.files[0];
                    if (!file) return;
                    new window.Compressor(file, {
                        quality: 0.6, maxWidth: 150,
                        success(result) {
                            const reader = new FileReader();
                            reader.onload = (e) => itemForm.image = e.target.result;
                            reader.readAsDataURL(result);
                        }
                    });
                };

                // --- 表格展示计算 ---
                const getDisplayRecords = (item) => {
                    const sorted = [...(item.history || [])].sort((a, b) => new Date(b.date) - new Date(a.date));
                    const merged = [];
                    
                    for (const rec of sorted) {
                        if (merged.length === 0) {
                            merged.push({ date: rec.date, values: { ...rec.values }, _merged: false });
                            continue;
                        }
                        
                        const last = merged[merged.length - 1];
                        const diffMs = Math.abs(new Date(last.date).getTime() - new Date(rec.date).getTime());
                        
                        // 依据 Param ID 进行冲突检测
                        if (diffMs <= 7200000) {
                            let canMerge = true;
                            for (const [pId, v] of Object.entries(rec.values)) {
                                if (v !== undefined && v !== null && v !== '') {
                                    if (last.values[pId] !== undefined && last.values[pId] !== null && last.values[pId] !== '') {
                                        canMerge = false;
                                        break;
                                    }
                                }
                            }
                            
                            if (canMerge) {
                                for (const [pId, v] of Object.entries(rec.values)) {
                                    if (v !== undefined && v !== null && v !== '') {
                                        last.values[pId] = v;
                                    }
                                }
                                last._merged = true;
                            } else {
                                merged.push({ date: rec.date, values: { ...rec.values }, _merged: false });
                            }
                        } else {
                            merged.push({ date: rec.date, values: { ...rec.values }, _merged: false });
                        }
                    }
                    return merged;
                };

                const getTrendIcon = (currentIndex, paramId, displayData) => {
                    const current = displayData[currentIndex]?.values[paramId];
                    const prev = displayData[currentIndex + 1]?.values[paramId];
                    if (current === undefined || current === '' || prev === undefined || prev === '') return '';
                    const diff = Number(current) - Number(prev);
                    if (diff > 0) return '<span class="text-red-500 text-xs ml-1">↑</span>';
                    if (diff < 0) return '<span class="text-green-500 text-xs ml-1">↓</span>';
                    return '<span class="text-gray-300 text-xs ml-1">-</span>';
                };

                const formatDate = (dateStr) => {
                    if (!dateStr || dateStr === '-') return '-';
                    const d = new Date(dateStr);
                    if (isNaN(d)) return dateStr;
                    const pad = n => String(n).padStart(2, '0');
                    return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
                };

                const formatForExcel = (dateStr) => {
                    if (!dateStr || dateStr === '-') return '-';
                    const d = new Date(dateStr);
                    if (isNaN(d)) return dateStr;
                    const pad = n => String(n).padStart(2, '0');
                    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
                };

                const guessEmoji = (name) => {
                    if(/电池|电量/.test(name)) return '🔋';
                    if(/体重|体脂/.test(name)) return '⚖️';
                    if(/血压|心率/.test(name)) return '❤️';
                    return '📊';
                };

                // --- 导入导出 (映射底层ID与展示名称) ---
                const exportCSV = () => {
                    if (items.value.length === 0) return alert("没有数据可导出！");
                    
                    const rows = [['项目名称', '记录时间', '参数名', '参数数值']];
                    
                    items.value.forEach(item => {
                        if (item.history && item.history.length > 0) {
                            item.history.forEach(record => {
                                Object.entries(record.values).forEach(([pId, value]) => {
                                    if (value !== null && value !== undefined && value !== '') {
                                        // 反查 ID 对应的名称
                                        const paramObj = item.params.find(p => p.id === pId);
                                        if (paramObj) {
                                            rows.push([item.name, formatForExcel(record.date), paramObj.name, value]);
                                        }
                                    }
                                });
                            });
                        } else {
                            rows.push([item.name, '-', '-', '-']);
                        }
                    });

                    const csvContent = "\ufeff" + rows.map(e => e.join(",")).join("\n");
                    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url; 
                    a.download = `数据记录_${new Date().toLocaleDateString()}.csv`;
                    a.click();
                };

                const importCSV = (e) => {
                    const file = e.target.files[0];
                    if (!file) return;

                    const reader = new FileReader();
                    reader.onload = (event) => {
                        try {
                            const text = event.target.result;
                            const lines = text.split(/\r?\n/);
                            if (lines.length < 2) throw new Error("无有效数据");

                            const tempMap = {};
                            
                            for (let i = 1; i < lines.length; i++) {
                                if (!lines[i].trim()) continue;
                                const cols = lines[i].split(",");
                                if (cols.length < 4) continue;

                                const [name, dateStr, paramName, val] = cols.map(c => c.trim());
                                if (!name) continue;

                                if (!tempMap[name]) {
                                    tempMap[name] = {
                                        id: generateId('item'),
                                        name: name,
                                        paramsList: [],
                                        paramsDict: {}, // {"名称": "ID"} 查重用
                                        image: '', 
                                        historyDict: {} 
                                    };
                                }

                                if (paramName && paramName !== '-') {
                                    // 按需生成和复用 ID
                                    let pId = tempMap[name].paramsDict[paramName];
                                    if (!pId) {
                                        pId = generateId('p');
                                        tempMap[name].paramsDict[paramName] = pId;
                                        tempMap[name].paramsList.push({ id: pId, name: paramName });
                                    }
                                    
                                    let isoDate = '-';
                                    if (dateStr && dateStr !== '-') {
                                        const parsedDate = new Date(dateStr.replace(/-/g, '/'));
                                        isoDate = isNaN(parsedDate) ? dateStr : parsedDate.toISOString();
                                    }

                                    if (isoDate && isoDate !== '-') {
                                        if (!tempMap[name].historyDict[isoDate]) tempMap[name].historyDict[isoDate] = {};
                                        if (val !== '-' && !isNaN(parseFloat(val))) {
                                            tempMap[name].historyDict[isoDate][pId] = parseFloat(val);
                                        }
                                    }
                                }
                            }

                            const newItems = Object.values(tempMap).map(item => ({
                                id: item.id,
                                name: item.name,
                                params: item.paramsList,
                                image: item.image,
                                history: Object.keys(item.historyDict).map(d => ({
                                    date: d,
                                    values: item.historyDict[d]
                                }))
                            }));

                            if (confirm(`识别到 ${newItems.length} 个项目，覆盖当前数据？（图片可能丢失）`)) {
                                items.value = newItems;
                                syncData();
                            }
                        } catch (err) { 
                            alert("CSV 解析失败，请确保格式为：项目名称,记录时间,参数名,参数数值"); 
                        }
                        e.target.value = '';
                    };
                    reader.readAsText(file);
                };

                onMounted(loadData);

                return {
                    items, modals, itemForm, recordForm, currentTargetItem,
                    openItemModal, addParam, removeParam, moveParam, saveItem, deleteItem, openRecordModal, saveRecord,
                    handleImage, getDisplayRecords, getTrendIcon, formatDate, guessEmoji,
                    exportCSV, importCSV
                };
            }
        }).mount('#app');
    </script>
    <style> [v-cloak] { display: none; } </style>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'supplies': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>日常用品追踪</title>
     
    <style>
        /* ... 保留之前的全部基础 CSS ... */
        :root { --primary: #4CAF50; --warning: #FF9800; --danger: #F44336; --bg: #f4f7f6; --card-bg: #ffffff; --text-main: #333; --text-sub: #777; }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text-main); padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; max-width: 800px; margin: 0 auto 20px; }
        .btn { background: var(--primary); color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 14px; }
        .btn.danger { background: var(--danger); }
        .container { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 15px; max-width: 800px; margin: 0 auto; }
        
        .card { background: var(--card-bg); border-radius: 12px; padding: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); position: relative; }
        
        /* 新增：图片与标题布局 */
        .card-header { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
        .item-thumb { width: 48px; height: 48px; border-radius: 8px; object-fit: cover; background: #eee; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 24px;}
        .card-title-area { flex-grow: 1; }
        
        .card-title { font-size: 18px; font-weight: bold; margin-bottom: 2px; }
        .card-info { font-size: 13px; color: var(--text-sub); }
        .badge { position: absolute; top: 15px; right: 15px; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: bold; }
        .badge.safe { background: #e8f5e9; color: #2e7d32; }
        .badge.warning { background: #fff3e0; color: #ef6c00; }
        .badge.danger { background: #ffebee; color: #c62828; }
        
        .progress-bar { width: 100%; height: 8px; background: #eee; border-radius: 4px; overflow: hidden; margin-bottom: 15px; }
        .progress-fill { height: 100%; background: var(--primary); transition: width 0.3s; }
        .progress-fill.warning { background: var(--warning); }
        .progress-fill.danger { background: var(--danger); }
        
        .card-actions { display: flex; justify-content: flex-end; gap: 10px; }
        
        .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.5); display: none; justify-content: center; align-items: center; z-index: 1000; }
        .modal { background: white; padding: 20px; border-radius: 12px; width: 90%; max-width: 400px; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; font-size: 14px; }
        .form-group input[type="text"], .form-group input[type="number"] { width: 100%; padding: 8px; border: 1px solid #ccc; border-radius: 6px; }
        .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }
        .btn-cancel { background: #e0e0e0; color: #333; }
        
        /* 图片上传按钮样式 */
        .upload-btn-wrapper { display: flex; align-items: center; gap: 10px; }
        .upload-btn-wrapper input[type="file"] { display: none; }
        .custom-upload-btn { background: #f0f0f0; border: 1px dashed #ccc; padding: 8px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
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

 
        <div class="header">
    <h2>📦 用品消耗看板</h2>
    <div style="display: flex; gap: 8px;">
        <button class="btn btn-cancel" onclick="exportToCSV()" title="备份数据">导出</button>
        <button class="btn btn-cancel" onclick="document.getElementById('csv-upload').click()" title="恢复数据">导入</button>
        <input type="file" id="csv-upload" accept=".csv" style="display:none" onchange="importFromCSV(event)">
        <button class="btn" onclick="openModal()">+ 添加</button>
    </div>
 
        
    </div>

    <div class="container" id="list-container">
        <p style="text-align: center; grid-column: 1/-1; color: #777;">加载中...</p>
    </div>

    <div class="modal-overlay" id="modal">
        <div class="modal">
            <h3 id="modal-title" style="margin-bottom: 15px;">添加物品</h3>
            <input type="hidden" id="item-id">
            
            <div class="form-group">
                <label>物品图片 (可选)</label>
                <div class="upload-btn-wrapper">
                    <label class="custom-upload-btn" for="item-image">📷 拍照 / 选图</label>
                    <input type="file" id="item-image" accept="image/*" capture="environment" onchange="handleImageUpload(event)">
                    <img id="image-preview" src="" style="display:none; width: 40px; height: 40px; border-radius: 6px; object-fit: cover;">
                </div>
                <input type="hidden" id="item-image-base64"> </div>

            <div class="form-group">
                <label>物品名称 (如: 牙线)</label>
                <input type="text" id="item-name" placeholder="输入名称">
            </div>
            <div class="form-group">
                <label>当前总数量/容量</label>
                <input type="number" step="0.01" id="item-stock" placeholder="如: 100">
            </div>
            <div class="form-group">
                <label>每天消耗量</label>
                <input type="number" step="0.01" id="item-daily" placeholder="如: 2">
            </div>
            <div class="modal-actions">
                <button class="btn btn-cancel" onclick="closeModal()">取消</button>
                <button class="btn" onclick="saveItem()">保存配置</button>
            </div>
        </div>
    </div>

    <script>
        const API_URL = API_BASE + '/api2/daily_supplies';
        let itemsData = [];

        // --- 图片处理逻辑：前端压缩图片并转为 Base64 ---
        function handleImageUpload(event) {
            const file = event.target.files[0];
            if (!file) return;

            const reader = new FileReader();
            reader.onload = function(e) {
                const img = new Image();
                img.onload = function() {
                    const canvas = document.createElement('canvas');
                    const MAX_SIZE = 150; 
                    let width = img.width;
                    let height = img.height;

                    if (width > height) {
                        if (width > MAX_SIZE) { height *= MAX_SIZE / width; width = MAX_SIZE; }
                    } else {
                        if (height > MAX_SIZE) { width *= MAX_SIZE / height; height = MAX_SIZE; }
                    }
                    canvas.width = width;
                    canvas.height = height;

                    const ctx = canvas.getContext('2d');
                    ctx.drawImage(img, 0, 0, width, height);

                    const dataUrl = canvas.toDataURL('image/jpeg', 0.7);
                    
                    document.getElementById('image-preview').src = dataUrl;
                    document.getElementById('image-preview').style.display = 'block';
                    document.getElementById('item-image-base64').value = dataUrl;
                }
                img.src = e.target.result;
            }
            reader.readAsDataURL(file);
        }

        // --- 简单 AI 文本推断 Emoji 的保底机制 ---
        function guessEmoji(name) {
            if(name.includes('牙线') || name.includes('牙刷') || name.includes('牙膏')) return '🦷';
            if(name.includes('纸') || name.includes('巾')) return '🧻';
            if(name.includes('水') || name.includes('液')) return '💧';
            if(name.includes('药') || name.includes('维C')) return '💊';
            if(name.includes('袋') || name.includes('垃圾')) return '🗑️';
            if(name.includes('粮') || name.includes('猫') || name.includes('狗')) return '🐾';
            return '📦';
        }

        async function loadData() {
            try {
                const res = await fetch(API_URL);
                if (res.status === 403) { window.location.href = API_BASE + '/'; return; }
                const data = await res.json();
                if (data && data.length > 0 && data[0].items) {
                    itemsData = data[0].items;
                } else {
                    itemsData = [];
                }
                renderList();
            } catch (err) {
                document.getElementById('list-container').innerHTML = '<p style="color:red;">加载失败</p>';
            }
        }

        async function syncToServer() {
            await fetch(API_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items: itemsData })
            });
            renderList();
        }

        // 核心修正：基于毫秒的实时扣减计算
        function calculateStatus(item) {
            const now = new Date();
            const startDate = new Date(item.last_updated || now);
            
            // 计算时间差，转为天数（带小数），实现平滑连续扣减
            const diffDays = Math.max(0, (now - startDate) / (1000 * 60 * 60 * 24));
            let currentRemaining = Math.max(0, parseFloat((item.stock - (diffDays * item.daily_use)).toFixed(2)));
            let daysLeft = item.daily_use > 0 ? Math.floor(currentRemaining / item.daily_use) : 999;
            let percent = item.stock > 0 ? Math.min(100, Math.max(0, (currentRemaining / item.stock) * 100)) : 0;

            let status = 'safe', statusText = '充足';
            if (daysLeft < 7 && daysLeft > 0) { status = 'warning'; statusText = '⚠️ 需采购'; } 
            else if (daysLeft <= 0) { status = 'danger'; statusText = '🚨 已耗尽'; }
            return { currentRemaining, daysLeft, percent, status, statusText };
        }

        function renderList() {
            const container = document.getElementById('list-container');
            container.innerHTML = '';
            if (itemsData.length === 0) {
                container.innerHTML = '<p style="text-align: center; grid-column: 1/-1; color: #777;">暂无追踪物品，请点击右上角添加。</p>';
                return;
            }

            itemsData.forEach(item => {
                const stats = calculateStatus(item);
                const imageContent = item.image 
                    ? `<img src="${item.image}" class="item-thumb">` 
                    : `<div class="item-thumb">${guessEmoji(item.name)}</div>`;

                const card = document.createElement('div');
                card.className = 'card';
                card.innerHTML = `
                    <div class="badge ${stats.status}">${stats.statusText}</div>
                    <div class="card-header">
                        ${imageContent}
                        <div class="card-title-area">
                            <div class="card-title">${item.name}</div>
                            <div class="card-info" style="margin:0;">每天耗: ${item.daily_use}</div>
                        </div>
                    </div>
                    <div class="card-info" style="margin-bottom:8px;">
                        当前剩余: <strong style="font-size:16px; color:#333;">${stats.currentRemaining}</strong>
                        <span style="float:right;">预计可用: <strong>${stats.daysLeft}</strong> 天</span>
                    </div>
                    <div class="progress-bar">
                        <div class="progress-fill ${stats.status}" style="width: ${stats.percent}%"></div>
                    </div>
                    <div class="card-actions">
                        <button class="btn btn-cancel" style="padding: 4px 8px; font-size:12px;" onclick="editItem('${item.id}')">编辑/补货</button>
                        <button class="btn danger" style="padding: 4px 8px; font-size:12px;" onclick="deleteItem('${item.id}')">删除</button>
                    </div>
                `;
                container.appendChild(card);
            });
        }

        const modal = document.getElementById('modal');
        
        function openModal() {
            document.getElementById('modal-title').innerText = '添加物品';
            document.getElementById('item-id').value = '';
            document.getElementById('item-name').value = '';
            document.getElementById('item-stock').value = '';
            document.getElementById('item-daily').value = '';
            document.getElementById('item-image').value = ''; 
            document.getElementById('item-image-base64').value = ''; 
            document.getElementById('image-preview').style.display = 'none';
            modal.style.display = 'flex';
        }

        function closeModal() { modal.style.display = 'none'; }

        function editItem(id) {
            const item = itemsData.find(i => i.id === id);
            if (!item) return;
            const stats = calculateStatus(item);

            document.getElementById('modal-title').innerText = '编辑 / 补货';
            document.getElementById('item-id').value = item.id;
            document.getElementById('item-name').value = item.name;
            // 补货时输入框默认填入实时计算后的当前量
            document.getElementById('item-stock').value = stats.currentRemaining; 
            document.getElementById('item-daily').value = item.daily_use;
            
            if (item.image) {
                document.getElementById('item-image-base64').value = item.image;
                document.getElementById('image-preview').src = item.image;
                document.getElementById('image-preview').style.display = 'block';
            } else {
                document.getElementById('item-image-base64').value = '';
                document.getElementById('image-preview').style.display = 'none';
            }
            modal.style.display = 'flex';
        }

        function saveItem() {
            const id = document.getElementById('item-id').value;
            const name = document.getElementById('item-name').value.trim();
            const stock = parseFloat(document.getElementById('item-stock').value);
            const daily = parseFloat(document.getElementById('item-daily').value);
            const imageBase64 = document.getElementById('item-image-base64').value; 

            if (!name || isNaN(stock) || isNaN(daily)) {
                alert("请填写完整的有效数据！");
                return;
            }

            // 保存时直接记录当前精确时间作为扣减基准点
            const nowStr = new Date().toISOString();

            if (id) {
                const index = itemsData.findIndex(i => i.id === id);
                if (index > -1) {
                    itemsData[index] = { ...itemsData[index], name, stock, daily_use: daily, last_updated: nowStr, image: imageBase64 };
                }
            } else {
                itemsData.push({
                    id: Date.now().toString(),
                    name, stock, daily_use: daily, last_updated: nowStr, image: imageBase64
                });
            }

            closeModal();
            syncToServer();
        }

        function deleteItem(id) {
            if (confirm("确定要删除这个记录吗？")) {
                itemsData = itemsData.filter(i => i.id !== id);
                syncToServer();
            }
        }

        // ================= CSV 导出功能 =================
        function exportToCSV() {
            if (itemsData.length === 0) {
                alert("没有数据可导出！");
                return;
            }

            const headers = ["名称", "当前库存", "每日消耗", "上次更新时间"];
            
            const rows = itemsData.map(item => {
                const stats = calculateStatus(item);
                return [
                    item.name,
                    stats.currentRemaining, // 导出实时剩余量
                    item.daily_use,
                    new Date().toISOString() // 修正：既然写入了计算后的当前剩余量，时间必须重置为当前，否则导入后会再次扣除时间差
                ].join(",");
            });

            const csvContent = "\ufeff" + [headers.join(","), ...rows].join("\n"); 
            const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
            const link = document.createElement("a");
            const url = URL.createObjectURL(blob);
            
            link.setAttribute("href", url);
            link.setAttribute("download", `用品库存备份_${new Date().toLocaleDateString()}.csv`);
            link.style.visibility = 'hidden';
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
        }

        // ================= CSV 导入功能 =================
        function importFromCSV(event) {
            const file = event.target.files[0];
            if (!file) return;

            const reader = new FileReader();
            reader.onload = function(e) {
                const text = e.target.result;
                const lines = text.split(/\r?\n/);
                
                if (lines.length < 2) return;

                const newItems = [];
                for (let i = 1; i < lines.length; i++) {
                    const cols = lines[i].split(",");
                    if (cols.length < 3) continue;

                    const name = cols[0].trim();
                    const stock = parseFloat(cols[1]);
                    const daily = parseFloat(cols[2]);
                    const lastUpdated = cols[3] ? cols[3].trim() : new Date().toISOString();

                    if (name && !isNaN(stock) && !isNaN(daily)) {
                        newItems.push({
                            id: (Date.now() + i).toString(), 
                            name: name,
                            stock: stock,
                            daily_use: daily,
                            last_updated: lastUpdated,
                            image: "" 
                        });
                    }
                }

                if (confirm(`识别到 ${newItems.length} 条数据，是否覆盖当前列表？`)) {
                    itemsData = newItems;
                    syncToServer();
                    alert("导入成功！");
                }
                event.target.value = '';
            };
            reader.readAsText(file);
        }
        
        loadData();
    </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'assert': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>高价值资产与生命周期追踪</title>
    <script src="static/cdn/tailwindcss3.4.17.js"></script>
    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <script src="static/cdn/echarts.min.js"></script>
    <script src="static/cdn/marked.min.js"></script>
    <script src="static/cdn/compressor.min.js"></script>
    <style>
        [v-cloak] { display: none; }
        .markdown-body { font-size: 0.875rem; line-height: 1.6; color: #374151; }
        .markdown-body h1, .markdown-body h2, .markdown-body h3 { font-weight: 600; margin-top: 0.75em; margin-bottom: 0.25em; color: #111827; }
        .markdown-body ul { list-style-type: disc; padding-left: 1.5em; }
        .markdown-body a { color: #3b82f6; text-decoration: none; }
        .markdown-body blockquote { border-left: 3px solid #cbd5e1; padding-left: 1em; color: #64748b; background: #f8fafc; padding: 0.25em 1em; border-radius: 2px;}
        .markdown-body p { margin-bottom: 0.5em; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: transparent; }
        ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
        .no-scrollbar::-webkit-scrollbar { display: none; }
        .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
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
<body class="bg-slate-100 text-slate-800 p-4 md:p-6 font-sans">
    <div id="app" class="mx-auto" style="max-width: 1000px;" v-cloak>
        <header class="flex flex-col md:flex-row justify-between items-center mb-4 gap-4">
            <h2 class="text-2xl font-bold flex items-center gap-2">资产与生命周期管理中枢</h2>
            <div class="flex gap-2 flex-wrap justify-end">
                <button @click="exportCSVView" class="px-4 py-2 bg-emerald-50 text-emerald-700 border border-emerald-200 hover:bg-emerald-100 rounded shadow-sm text-sm transition-colors">📊 导出 CSV 报表</button>
                <div class="w-px bg-slate-300 mx-1"></div>
                <button @click="exportJSONBackup" class="px-4 py-2 bg-white border border-slate-200 hover:bg-slate-50 rounded shadow-sm text-sm">导出 JSON 备份</button>
                <button @click="$refs.fileInput.click()" class="px-4 py-2 bg-white border border-slate-200 hover:bg-slate-50 rounded shadow-sm text-sm">导入 JSON 恢复</button>
                <input type="file" ref="fileInput" accept=".json" class="hidden" @change="importJSON">
                <div class="w-px bg-slate-300 mx-1"></div>
                <button @click="openItemModal()" class="px-4 py-2 bg-slate-800 hover:bg-slate-900 text-white rounded shadow-sm text-sm">+ 录入新资产</button>
            </div>
        </header>

        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm flex flex-col justify-between">
                <div class="text-xs text-slate-500 mb-1">资金总投入 (买入+附加)</div>
                <div class="text-xl font-bold text-slate-800">¥{{ globalStats.totalInvest }}</div>
                <div class="text-[10px] text-slate-400 mt-2">共 {{ items.length }} 件追踪中资产</div>
            </div>
            <div class="bg-white p-4 rounded-xl border border-slate-200 shadow-sm flex flex-col justify-between">
                <div class="text-xs text-slate-500 mb-1">当前总净值 (随时变现)</div>
                <div class="text-xl font-bold text-slate-800">¥{{ globalStats.totalNetValue }}</div>
                <div class="text-[10px] text-slate-400 mt-2">按最新二手估价计算</div>
            </div>
            <div @click="openBreakdownModal('tco')" class="bg-white p-4 rounded-xl border-2 border-transparent hover:border-blue-300 shadow-sm flex flex-col justify-between relative group cursor-pointer transition-colors">
                <div class="text-xs text-slate-500 mb-1 flex justify-between items-center">
                    <span>累计真实 TCO</span>
                    <span class="text-[10px] text-blue-500 bg-blue-50 px-1.5 py-0.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">点看明细</span>
                </div>
                <div class="text-xl font-bold text-blue-600">¥{{ globalStats.totalTCO }}</div>
                <div class="text-[10px] text-slate-500 mt-2 border-t pt-1 border-slate-100">
                    近一年流血: <span class="font-bold text-blue-600">¥{{ globalStats.globalDailyTCO }}</span>/日
                </div>
            </div>
            <div @click="openBreakdownModal('usage')" class="bg-white p-4 rounded-xl border-2 border-transparent hover:border-red-300 shadow-sm flex flex-col justify-between relative group cursor-pointer transition-colors">
                <div class="text-xs text-slate-500 mb-1 flex justify-between items-center">
                    <span>累计运行与事故</span>
                    <span class="text-[10px] text-red-500 bg-red-50 px-1.5 py-0.5 rounded opacity-0 group-hover:opacity-100 transition-opacity">点看明细</span>
                </div>
                <div class="text-xl font-bold text-red-500">¥{{ globalStats.totalUsage }}</div>
                <div class="text-[10px] text-slate-500 mt-2 border-t pt-1 border-slate-100">
                    近一年开销: <span class="font-bold text-red-500">¥{{ globalStats.globalDailyUsage }}</span>/日
                </div>
            </div>
        </div>

        <div class="flex gap-2 overflow-x-auto no-scrollbar mb-6 pb-1 border-b border-slate-200">
            <button v-for="tab in tabs" :key="tab.id" @click="currentTab = tab.id"
                class="px-4 py-2 text-sm font-medium rounded-t-lg transition-colors whitespace-nowrap border-b-2"
                :class="currentTab === tab.id ? 'border-slate-800 text-slate-800' : 'border-transparent text-slate-500 hover:text-slate-700 hover:bg-slate-50'">
                {{ tab.name }} <span class="ml-1 text-[10px] bg-slate-100 px-1.5 py-0.5 rounded text-slate-500">{{ getCategoryCount(tab.id) }}</span>
            </button>
        </div>

        <div v-if="filteredItems.length === 0" class="text-center text-slate-400 py-20 bg-white rounded-xl shadow-sm border border-slate-200">
            当前分类下暂无资产记录。
        </div>

        <div class="grid gap-6 grid-cols-1 md:grid-cols-2 items-start">
            <div v-for="item in filteredItems" :key="item.id" class="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden flex flex-col hover:shadow-md transition-shadow">
                
                <div class="p-5 flex gap-4 relative">
                    <span :class="getStatusColor(item.status)" class="absolute top-4 right-4 text-[10px] px-2 py-0.5 rounded-full font-bold border">
                        {{ getStatusText(item.status) }}
                    </span>
                    <img v-if="item.image" :src="item.image" @click="zoomImage(item.image)" class="w-20 h-20 rounded-lg object-cover bg-slate-50 border cursor-zoom-in shrink-0">
                    <div v-else class="w-20 h-20 rounded-lg bg-slate-50 border flex items-center justify-center text-3xl shrink-0">{{ guessEmoji(item.name) }}</div>
                    
                    <div class="flex-grow pt-1">
                        <h3 class="font-bold text-lg mb-1 pr-12 truncate select-text" :title="item.name">{{ item.name }}</h3>
                        <div class="grid grid-cols-2 gap-x-2 gap-y-1 text-xs text-slate-500">
                            <div class="select-text">买入: <span class="text-slate-800">¥{{ item.purchasePrice || 0 }}</span></div>
                            <div class="select-text">日期: <span class="text-slate-800">{{ item.purchaseDate || '-' }}</span></div>
                            <div>保修: <span :class="getWarrantyStatus(item).color">{{ getWarrantyStatus(item).text }}</span></div>
                        </div>
                    </div>
                </div>

                <div class="px-5 py-3 bg-[#fbfbf9] border-y border-slate-100 shadow-inner">
                    <div class="flex justify-between items-center mb-2">
                        <span class="text-xs font-semibold text-slate-500 flex items-center gap-1">📝 详细备忘</span>
                        <button @click="toggleEditDesc(item)" class="text-xs px-2 py-0.5 rounded" :class="editingDesc[item.id] ? 'bg-blue-500 text-white hover:bg-blue-600' : 'text-blue-500 hover:bg-blue-50 border border-blue-200'">
                            {{ editingDesc[item.id] ? '保存' : '编辑' }}
                        </button>
                    </div>
                    
                    <div v-if="editingDesc[item.id]">
                        <textarea v-model="item.description" rows="6" class="w-full p-2 border rounded text-sm font-mono outline-none focus:ring-2 focus:ring-blue-100 bg-white" placeholder="支持 Markdown 格式..."></textarea>
                    </div>
                    <div v-else class="markdown-body max-h-[160px] overflow-y-auto select-text" v-html="renderMarkdown(item.description) || '<span class=\'text-slate-400 text-xs italic\'>暂无备忘信息...</span>'"></div>
                </div>

                <div class="border-b border-slate-100">
                    <div class="flex justify-between items-start px-5 py-3 hover:bg-slate-50 transition-colors group cursor-pointer" @click="toggleChart(item)">
                        <div class="flex flex-col gap-1.5 flex-grow">
                            <span class="text-xs font-semibold text-slate-600 flex items-center gap-1">📈 核心数据与折合日常花费</span>
                            <div class="flex flex-wrap items-center gap-1.5 text-[10px]" @click.stop>
                                <span class="bg-slate-100 px-1.5 py-0.5 rounded border border-slate-200 cursor-text select-text text-slate-700">估价: ¥{{ getLatestPrice(item) }}</span>
                                <span class="bg-blue-50 px-1.5 py-0.5 rounded border border-blue-200 cursor-text select-text text-blue-700 font-medium">近一年TCO: ¥{{ getAssetTtmStats(item).dailyTco.toFixed(2) }}/日</span>
                                <span class="bg-red-50 px-1.5 py-0.5 rounded border border-red-200 cursor-text select-text text-red-600 font-medium">近一年运行: ¥{{ getAssetTtmStats(item).dailyUsage.toFixed(2) }}/日</span>
                            </div>
                        </div>
                        <button class="text-slate-400 hover:text-slate-700 text-xs py-1 px-2 -mr-2 focus:outline-none shrink-0 self-center">
                            {{ expandedCharts[item.id] ? '▲ 收起' : '▼ 展开' }}
                        </button>
                    </div>
                    
                    <div v-if="expandedCharts[item.id]" class="px-5 pb-3 bg-white">
                        <div class="grid grid-cols-2 md:grid-cols-4 gap-2 mb-3 text-center border-b border-slate-50 pb-3">
                            <div class="bg-slate-50 p-1.5 rounded relative group select-text border border-slate-100 flex flex-col justify-center">
                                <div class="text-[10px] text-slate-500 mb-0.5">终生真实 TCO</div>
                                <div class="text-xs font-bold text-slate-700">¥{{ calculateTCO(item) }}</div>
                                <div class="text-[10px] text-slate-400 mt-1 pt-1 border-t border-slate-200">均摊: ¥{{ getDailyTCO(item) }}/日</div>
                            </div>
                            <div class="bg-blue-50 p-1.5 rounded relative group select-text border border-blue-100 flex flex-col justify-center">
                                <div class="text-[10px] text-blue-600 font-bold mb-0.5">🎯 近一年 TCO</div>
                                <div class="text-xs font-bold text-blue-800">¥{{ getAssetTtmStats(item).tco.toFixed(2) }}</div>
                                <div class="text-[10px] text-blue-500 mt-1 pt-1 border-t border-blue-200">流血: ¥{{ getAssetTtmStats(item).dailyTco.toFixed(2) }}/日</div>
                            </div>
                            <div class="bg-slate-50 p-1.5 rounded relative group select-text border border-slate-100 flex flex-col justify-center">
                                <div class="text-[10px] text-slate-500 mb-0.5">终生运行与事故</div>
                                <div class="text-xs font-bold text-slate-700">¥{{ getUsageCost(item) }}</div>
                                <div class="text-[10px] text-slate-400 mt-1 pt-1 border-t border-slate-200">均摊: ¥{{ getDailyUsage(item) }}/日</div>
                            </div>
                            <div class="bg-red-50 p-1.5 rounded relative group select-text border border-red-100 flex flex-col justify-center">
                                <div class="text-[10px] text-red-600 font-bold mb-0.5">🔥 近一年运行</div>
                                <div class="text-xs font-bold text-red-800">¥{{ getAssetTtmStats(item).usage.toFixed(2) }}</div>
                                <div class="text-[10px] text-red-500 mt-1 pt-1 border-t border-red-200">花销: ¥{{ getAssetTtmStats(item).dailyUsage.toFixed(2) }}/日</div>
                            </div>
                        </div>

                        <div class="flex justify-between items-center mb-2 px-1">
                            <div class="text-[10px] text-slate-500">平台期预测: <span class="font-bold" :class="getPlateauStatus(item).color">{{ getPlateauStatus(item).text }}</span></div>
                            <div class="inline-flex bg-slate-100 rounded p-1 gap-1">
                                <button @click="switchChartMode(item, 'net')" :class="chartMode[item.id] !== 'tco' ? 'bg-white shadow-sm text-slate-800' : 'text-slate-500 hover:text-slate-700'" class="px-2.5 py-0.5 text-[10px] font-medium rounded transition-colors">净值折线图</button>
                                <button @click="switchChartMode(item, 'tco')" :class="chartMode[item.id] === 'tco' ? 'bg-white shadow-sm text-slate-800' : 'text-slate-500 hover:text-slate-700'" class="px-2.5 py-0.5 text-[10px] font-medium rounded transition-colors">硬件TCO走势</button>
                            </div>
                        </div>

                        <div :ref="el => { if (el) chartsRefs[item.id] = el }" style="height: 140px; width: 100%;"></div>
                        <div v-if="!hasEnoughChartData(item)" class="text-center text-[10px] text-slate-400 -mt-6 relative z-10">数据量不足，继续添加估价记录以生成趋势图</div>
                    </div>
                </div>

                <div class="px-5 py-4 flex-grow max-h-[160px] overflow-y-auto bg-white">
                    <div class="text-xs font-semibold text-slate-400 mb-3">历史记录与开销分类</div>
                    <div v-if="getEvents(item).length === 0" class="text-xs text-slate-300">暂无事件记录</div>
                    <div class="relative border-l-2 border-slate-100 ml-2 space-y-4">
                        <div v-for="event in getEvents(item)" :key="event.id" class="relative pl-4 group">
                            <div class="absolute w-2 h-2 bg-slate-300 rounded-full -left-[5px] top-1.5 border-2 border-white"></div>
                            <div class="flex justify-between items-start">
                                <div>
                                    <div class="text-[10px] text-slate-400 mb-0.5">{{ formatDate(event.date) }}</div>
                                    <div class="text-sm font-medium text-slate-700 flex items-center gap-1.5 flex-wrap">
                                        <span v-if="event.type === 'event'" class="text-[10px] px-1.5 py-0.5 rounded border" :class="getCategoryStyle(event.category).class">{{ getCategoryStyle(event.category).text }}</span>
                                        <span class="select-text">{{ event.value }}</span>
                                        <span v-if="event.cost" class="text-red-500 font-bold text-xs select-text">(-¥{{ event.cost }})</span>
                                    </div>
                                    <div v-if="event.note" class="text-xs text-slate-500 mt-1 bg-slate-50 p-1.5 rounded inline-block select-text">{{ event.note }}</div>
                                </div>
                                <button @click="editRecord(item, event)" class="text-[10px] text-blue-500 opacity-0 group-hover:opacity-100 transition-opacity bg-blue-50 px-2 py-1 rounded shrink-0">修改</button>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="p-3 bg-slate-50 border-t border-slate-100 flex justify-between items-center mt-auto">
                    <div class="flex gap-2">
                        <button @click="openRecordModal(item, 'event')" class="px-3 py-1.5 bg-emerald-100 hover:bg-emerald-200 text-emerald-700 rounded text-xs font-medium transition-colors">记开销</button>
                        <button @click="openRecordModal(item, 'price')" class="px-3 py-1.5 bg-blue-100 hover:bg-blue-200 text-blue-700 rounded text-xs font-medium transition-colors">新估价</button>
                    </div>
                    <div class="flex gap-2">
                        <button v-if="item.receiptImage" @click="zoomImage(item.receiptImage)" class="px-2.5 py-1.5 border border-slate-200 bg-white hover:bg-slate-100 rounded text-xs text-slate-600">发票</button>
                        <button @click="openItemModal(item)" class="px-2.5 py-1.5 border border-slate-200 bg-white hover:bg-slate-100 rounded text-xs text-slate-600">属性</button>
                        <button @click="deleteItem(item.id)" class="px-2.5 py-1.5 text-red-500 hover:bg-red-50 rounded text-xs">删除</button>
                    </div>
                </div>
            </div>
        </div>

        <div v-if="modals.breakdown" class="fixed inset-0 bg-slate-900 bg-opacity-60 flex justify-center items-center z-[55] p-4">
            <div class="bg-white rounded-xl shadow-2xl w-full max-w-md overflow-hidden flex flex-col max-h-[85vh]">
                <div class="p-4 border-b border-slate-100 flex justify-between items-center bg-slate-50">
                    <h3 class="text-lg font-bold text-slate-800">{{ breakdownType === 'tco' ? '🩸 近一年 TCO 每日流血榜' : '🔥 近一年使用开销榜' }}</h3>
                    <button @click="modals.breakdown = false" class="text-slate-400 hover:text-slate-700 text-2xl leading-none">&times;</button>
                </div>
                <div class="overflow-y-auto p-0 relative">
                    <table class="w-full text-sm text-left">
                        <thead class="bg-white text-xs text-slate-500 sticky top-0 border-b border-slate-200 z-10 shadow-sm">
                            <tr>
                                <th class="px-4 py-3 font-medium">资产名称</th>
                                <th class="px-4 py-3 font-medium text-right">近一年总花费</th>
                                <th class="px-4 py-3 font-medium text-right">折合每日</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-100">
                            <tr v-for="(b, idx) in breakdownList" :key="idx" class="hover:bg-slate-50 transition-colors">
                                <td class="px-4 py-3 text-slate-800 font-medium truncate max-w-[140px]" :title="b.name">
                                    <span class="text-xs text-slate-400 mr-1">{{ idx + 1 }}.</span> {{ b.name }}
                                </td>
                                <td class="px-4 py-3 text-right text-slate-600">¥{{ breakdownType === 'tco' ? b.tco.toFixed(2) : b.usage.toFixed(2) }}</td>
                                <td class="px-4 py-3 text-right font-bold" :class="breakdownType === 'tco' ? 'text-blue-600' : 'text-red-600'">
                                    ¥{{ breakdownType === 'tco' ? b.dailyTco.toFixed(2) : b.dailyUsage.toFixed(2) }}
                                </td>
                            </tr>
                            <tr v-if="breakdownList.length === 0">
                                <td colspan="3" class="px-4 py-12 text-center text-slate-400 text-sm">过去一年暂无产生此类开销的资产。</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="p-3 border-t border-slate-100 bg-slate-50 text-center text-[10px] text-slate-500">
                    注：采用 TTM 插值算法动态测算，非满一年的新资产按实际天数折合每日。
                </div>
            </div>
        </div>

        <div v-if="modals.item" class="fixed inset-0 bg-slate-900 bg-opacity-60 flex justify-center items-start z-40 p-4 overflow-y-auto" @paste="handlePaste">
            <div class="bg-white rounded-xl shadow-2xl w-full max-w-2xl my-8 flex flex-col">
                <div class="p-5 border-b border-slate-100 flex justify-between items-center sticky top-0 bg-white rounded-t-xl z-10">
                    <h3 class="text-xl font-bold">{{ itemForm.id ? '编辑资产基础属性' : '录入新资产' }}</h3>
                    <button @click="modals.item = false" class="text-slate-400 hover:text-slate-700 text-2xl leading-none">&times;</button>
                </div>
                <div class="p-6 space-y-5 overflow-y-auto">
                    <div class="grid grid-cols-1 md:grid-cols-2 gap-5">
                        <div class="space-y-4">
                            <div>
                                <label class="block text-sm font-semibold mb-1 text-slate-700">资产名称 <span class="text-red-500">*</span></label>
                                <input v-model="itemForm.name" type="text" class="w-full p-2 border rounded-lg focus:ring-2 focus:ring-blue-100 outline-none">
                            </div>
                            <div class="grid grid-cols-2 gap-3">
                                <div class="col-span-2">
                                    <label class="block text-sm font-semibold mb-1 text-slate-700">所属分类</label>
                                    <select v-model="itemForm.baseCategory" class="w-full p-2 border rounded-lg outline-none bg-white">
                                        <option v-for="tab in tabs.filter(t=>t.id!=='all')" :value="tab.id">{{ tab.name }}</option>
                                    </select>
                                </div>
                                <div>
                                    <label class="block text-sm font-semibold mb-1 text-slate-700">购入日期</label>
                                    <input v-model="itemForm.purchaseDate" type="date" class="w-full p-2 border rounded-lg outline-none">
                                </div>
                                <div>
                                    <label class="block text-sm font-semibold mb-1 text-slate-700">当前状态</label>
                                    <select v-model="itemForm.status" class="w-full p-2 border rounded-lg outline-none bg-white">
                                        <option value="active">在用 (良好)</option>
                                        <option value="idle">吃灰 (闲置)</option>
                                        <option value="repairing">故障 (待修)</option>
                                        <option value="sold">已出 (二手)</option>
                                    </select>
                                </div>
                            </div>
                            <div class="grid grid-cols-2 gap-3">
                                <div>
                                    <label class="block text-sm font-semibold mb-1 text-slate-700">买入价格 (¥)</label>
                                    <input v-model.number="itemForm.purchasePrice" type="number" class="w-full p-2 border rounded-lg outline-none">
                                </div>
                                <div>
                                    <label class="block text-sm font-semibold mb-1 text-slate-700">保修期 (月)</label>
                                    <input v-model.number="itemForm.warrantyMonths" type="number" class="w-full p-2 border rounded-lg outline-none">
                                </div>
                            </div>
                        </div>
                        <div class="space-y-4">
                            <div>
                                <label class="block text-sm font-semibold mb-1 text-slate-700">资产主图 <span class="text-xs font-normal text-slate-400 ml-2">(支持直接粘贴)</span></label>
                                <div class="flex items-center gap-3">
                                    <button @click="$refs.imageInput.click()" class="px-4 py-2 bg-slate-50 border border-dashed border-slate-300 rounded-lg text-sm hover:bg-slate-100 w-full text-left text-slate-500">📷 选图/粘贴</button>
                                    <input type="file" ref="imageInput" accept="image/*" class="hidden" @change="e => handleImageUpload(e, 'image')">
                                    <div class="relative group shrink-0">
                                        <img v-if="itemForm.image" :src="itemForm.image" class="w-12 h-12 rounded-lg object-cover border cursor-zoom-in" @click="zoomImage(itemForm.image)">
                                        <button v-if="itemForm.image" @click="itemForm.image = ''" class="absolute -top-2 -right-2 bg-red-500 text-white rounded-full w-5 h-5 text-xs hidden group-hover:flex items-center justify-center">&times;</button>
                                    </div>
                                </div>
                            </div>
                            <div>
                                <label class="block text-sm font-semibold mb-1 text-slate-700">单据/发票凭证</label>
                                <div class="flex items-center gap-3">
                                    <button @click="$refs.receiptInput.click()" class="px-4 py-2 bg-slate-50 border border-dashed border-slate-300 rounded-lg text-sm hover:bg-slate-100 w-full text-left text-slate-500">🧾 选图/粘贴</button>
                                    <input type="file" ref="receiptInput" accept="image/*" class="hidden" @change="e => handleImageUpload(e, 'receiptImage')">
                                    <div class="relative group shrink-0">
                                        <img v-if="itemForm.receiptImage" :src="itemForm.receiptImage" class="w-12 h-12 rounded-lg object-cover border cursor-zoom-in" @click="zoomImage(itemForm.receiptImage)">
                                        <button v-if="itemForm.receiptImage" @click="itemForm.receiptImage = ''" class="absolute -top-2 -right-2 bg-red-500 text-white rounded-full w-5 h-5 text-xs hidden group-hover:flex items-center justify-center">&times;</button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
                <div class="p-5 border-t border-slate-100 flex justify-end gap-3 bg-slate-50 rounded-b-xl">
                    <button @click="modals.item = false" class="px-5 py-2.5 bg-white border border-slate-200 rounded-lg text-sm font-medium hover:bg-slate-50">取消</button>
                    <button @click="saveItem" class="px-5 py-2.5 bg-slate-800 text-white rounded-lg text-sm font-medium hover:bg-slate-900 shadow-sm">保存</button>
                </div>
            </div>
        </div>

        <div v-if="modals.record" class="fixed inset-0 bg-slate-900 bg-opacity-60 flex justify-center items-center z-50 p-4">
            <div class="bg-white rounded-xl shadow-2xl w-full max-w-sm overflow-hidden">
                <div class="p-5 border-b border-slate-100">
                    <h3 class="text-lg font-bold">{{ recordForm.id ? '编辑记录' : (recordForm.type === 'price' ? '💰 更新二手估价' : '🔧 添加记录与开销') }}</h3>
                    <p class="text-xs text-slate-500 mt-1">目标: {{ currentTargetItem?.name }}</p>
                </div>
                <div class="p-5 space-y-4">
                    <div>
                        <label class="block text-sm font-semibold mb-1 text-slate-700">时间</label>
                        <input v-model="recordForm.date" type="datetime-local" class="w-full p-2 border rounded-lg outline-none">
                    </div>
                    
                    <template v-if="recordForm.type === 'price'">
                        <div>
                            <label class="block text-sm font-semibold mb-1 text-slate-700">当前二手残值评估 (¥)</label>
                            <input v-model.number="recordForm.value" type="number" placeholder="输入最新市价估值" class="w-full p-2 border rounded-lg outline-none focus:ring-2 focus:ring-blue-100">
                        </div>
                    </template>
                    
                    <template v-if="recordForm.type === 'event'">
                        <div>
                            <label class="block text-sm font-semibold mb-1 text-slate-700">记录类型与分类</label>
                            <select v-model="recordForm.category" class="w-full p-2 border rounded-lg outline-none bg-white focus:ring-2 focus:ring-emerald-100">
                                <option value="wear">🔧 损耗维保 (计入TCO)</option>
                                <option value="usage">🔋 日常使用 (独立计算)</option>
                                <option value="accessory">✨ 配件改装 (计入TCO)</option>
                                <option value="accident">💥 事故维修 (独立计算)</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm font-semibold mb-1 text-slate-700">记录内容</label>
                            <input v-model="recordForm.value" type="text" placeholder="如：更换机油、洗车、年检" class="w-full p-2 border rounded-lg outline-none focus:ring-2 focus:ring-emerald-100">
                        </div>
                        <div>
                            <label class="block text-sm font-semibold mb-1 text-slate-700">本次花费金额 (¥)</label>
                            <input v-model.number="recordForm.cost" type="number" placeholder="无花费留空" class="w-full p-2 border rounded-lg outline-none focus:ring-2 focus:ring-emerald-100">
                        </div>
                        <div>
                            <label class="block text-sm font-semibold mb-1 text-slate-700">补充备注 (可选)</label>
                            <input v-model="recordForm.note" type="text" placeholder="具体型号、里程等..." class="w-full p-2 border rounded-lg outline-none focus:ring-2 focus:ring-emerald-100">
                        </div>
                    </template>
                </div>
                <div class="p-4 border-t border-slate-100 flex justify-end gap-3 bg-slate-50">
                    <button @click="modals.record = false" class="px-4 py-2 bg-white border border-slate-200 rounded-lg text-sm font-medium">取消</button>
                    <button @click="saveRecord" class="px-4 py-2 text-white rounded-lg text-sm font-medium" :class="recordForm.type === 'price' ? 'bg-blue-600 hover:bg-blue-700' : 'bg-emerald-600 hover:bg-emerald-700'">保存</button>
                </div>
            </div>
        </div>

        <div v-if="zoomedImageSrc" class="fixed inset-0 bg-black bg-opacity-90 flex justify-center items-center z-[60] p-4 cursor-zoom-out" @click="zoomedImageSrc = ''">
            <img :src="zoomedImageSrc" class="max-w-full max-h-full object-contain rounded" @click.stop>
            <button @click="zoomedImageSrc = ''" class="absolute top-6 right-6 text-white text-4xl hover:text-slate-300">&times;</button>
        </div>
    </div>

    <script>
        const { createApp, ref, reactive, computed, onMounted, nextTick } = Vue;

        createApp({
            setup() {
                const API_URL = API_BASE + '/tracker_data/sync/assets';
                const items = ref([]);
                const modals = reactive({ item: false, record: false, breakdown: false });
                const currentTargetItem = ref(null);
                const zoomedImageSrc = ref('');
                
                const breakdownType = ref('tco');
                const editingDesc = reactive({});
                const expandedCharts = reactive({});
                const chartMode = reactive({}); 
                const chartsRefs = reactive({});
                const chartInstances = {};

                const tabs = [
                    { id: 'all', name: '全部分类' },
                    { id: 'vehicle', name: '载具及户外' },
                    { id: 'productivity', name: '生产力设备' },
                    { id: 'entertainment', name: '生活与娱乐' }
                ];
                const currentTab = ref('all');

                const itemForm = reactive({ id: '', name: '', baseCategory: 'productivity', purchaseDate: '', purchasePrice: 0, warrantyMonths: 0, status: 'active', description: '', image: '', receiptImage: '' });
                const recordForm = reactive({ id: '', type: 'price', date: '', value: '', note: '', cost: '', category: 'wear' });

                const generateId = (prefix) => prefix + '_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5);

                const loadData = async () => {
                    try {
                        const res = await fetch(API_URL);
                        if (res.status === 403) return window.location.href = API_BASE + '/';
                        const data = await res.json();
                        items.value = (data[0]?.items || []).map(i => {
                            if (i.history && i.purchaseDate) {
                                i.history.forEach(h => {
                                    if (h.type === 'price' && h.note === '购入价格') {
                                        h.date = new Date(i.purchaseDate).toISOString();
                                    }
                                });
                                i.history.sort((a, b) => new Date(b.date) - new Date(a.date));
                            }
                            return { ...i, baseCategory: i.baseCategory || 'productivity' };
                        });
                    } catch (err) {
                        items.value = [];
                    }
                };

                const syncData = async () => {
                    try {
                        await fetch(API_URL, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ items: items.value })
                        });
                    } catch (e) { console.error("同步失败", e); }
                };

                const getLatestPrice = (item, formatted = true) => {
                    const prices = (item.history || []).filter(h => h.type === 'price' && h.note !== '购入价格').sort((a, b) => new Date(b.date) - new Date(a.date));
                    if (prices.length > 0) return formatted ? prices[0].value.toLocaleString() : prices[0].value;
                    return formatted ? (item.purchasePrice ? item.purchasePrice.toLocaleString() : '-') : (item.purchasePrice || 0);
                };

                const filteredItems = computed(() => {
                    let list = currentTab.value === 'all' ? items.value : items.value.filter(i => i.baseCategory === currentTab.value);
                    return list.slice().sort((a, b) => {
                        const valA = getLatestPrice(a, false) || 0;
                        const valB = getLatestPrice(b, false) || 0;
                        return valB - valA;
                    });
                });

                const getCategoryCount = (id) => {
                    if (id === 'all') return items.value.length;
                    return items.value.filter(i => i.baseCategory === id).length;
                };

                const getAssetTtmStats = (item) => {
                    const now = new Date();
                    const oneYearAgo = new Date(now.getTime() - 365 * 24 * 60 * 60 * 1000);

                    let ttmHwCost = 0, ttmUsgCost = 0;
                    const allPrices = [];

                    if (item.purchaseDate && item.purchasePrice) {
                        allPrices.push({ date: new Date(item.purchaseDate).getTime(), value: Number(item.purchasePrice) });
                    }
                    (item.history || []).forEach(h => {
                        if (h.type === 'event' && h.cost && new Date(h.date) >= oneYearAgo) {
                            const cat = h.category || 'wear';
                            if (cat === 'wear' || cat === 'accessory') ttmHwCost += Number(h.cost);
                            if (cat === 'usage' || cat === 'accident') ttmUsgCost += Number(h.cost);
                        }
                        if (h.type === 'price' && h.note !== '购入价格') {
                            allPrices.push({ date: new Date(h.date).getTime(), value: Number(h.value) });
                        }
                    });

                    allPrices.sort((a, b) => a.date - b.date);

                    const netVal = getLatestPrice(item, false);
                    const currentNet = typeof netVal === 'number' ? netVal : 0;
                    let startPrice = currentNet; 

                    if (allPrices.length > 0) {
                        const t365 = oneYearAgo.getTime();
                        if (t365 <= allPrices[0].date) {
                            startPrice = allPrices[0].value;
                        } else if (t365 >= allPrices[allPrices.length - 1].date) {
                            startPrice = allPrices[allPrices.length - 1].value;
                        } else {
                            for (let i = 0; i < allPrices.length - 1; i++) {
                                if (allPrices[i].date <= t365 && allPrices[i+1].date >= t365) {
                                    const pA = allPrices[i], pB = allPrices[i+1];
                                    if (pA.date === pB.date) {
                                        startPrice = pB.value;
                                    } else {
                                        const ratio = (t365 - pA.date) / (pB.date - pA.date);
                                        startPrice = pA.value + (pB.value - pA.value) * ratio;
                                    }
                                    break;
                                }
                            }
                        }
                    }

                    const ttmTCO = (startPrice - currentNet) + ttmHwCost;
                    
                    let days = 365;
                    const pDate = item.purchaseDate ? new Date(item.purchaseDate).getTime() : 0;
                    if (pDate && pDate > oneYearAgo.getTime()) {
                        days = Math.max(1, Math.ceil((now.getTime() - pDate) / 86400000));
                    }

                    return {
                        tco: ttmTCO,
                        usage: ttmUsgCost,
                        dailyTco: (ttmTCO / days),
                        dailyUsage: (ttmUsgCost / days)
                    };
                };

                const globalStats = computed(() => {
                    let totalInvest = 0, totalNetValue = 0, totalTCO = 0, totalUsage = 0;
                    let globalTtmTCO = 0, globalTtmUsage = 0;
                    let breakdown = [];

                    items.value.forEach(item => {
                        const pPrice = item.purchasePrice || 0;
                        const hwCost = Number(getHardwareCost(item).replace(/,/g, ''));
                        const usgCost = Number(getUsageCost(item).replace(/,/g, ''));
                        const netVal = getLatestPrice(item, false);
                        const currentNet = typeof netVal === 'number' ? netVal : 0;
                        
                        totalInvest += (pPrice + hwCost);
                        totalNetValue += currentNet;
                        totalTCO += (pPrice + hwCost - currentNet);
                        totalUsage += usgCost;

                        const ttm = getAssetTtmStats(item);
                        globalTtmTCO += ttm.tco;
                        globalTtmUsage += ttm.usage;

                        if (ttm.tco > 0 || ttm.usage > 0) {
                            breakdown.push({
                                name: item.name,
                                tco: ttm.tco,
                                dailyTco: ttm.dailyTco,
                                usage: ttm.usage,
                                dailyUsage: ttm.dailyUsage
                            });
                        }
                    });

                    return {
                        totalInvest: totalInvest.toLocaleString(),
                        totalNetValue: totalNetValue.toLocaleString(),
                        totalTCO: totalTCO.toLocaleString(),
                        totalUsage: totalUsage.toLocaleString(),
                        globalDailyTCO: (globalTtmTCO / 365).toFixed(2),
                        globalDailyUsage: (globalTtmUsage / 365).toFixed(2),
                        breakdown
                    };
                });

                const breakdownList = computed(() => {
                    let list = [...globalStats.value.breakdown];
                    if (breakdownType.value === 'tco') {
                        return list.filter(i => i.tco !== 0).sort((a, b) => b.dailyTco - a.dailyTco);
                    } else {
                        return list.filter(i => i.usage !== 0).sort((a, b) => b.dailyUsage - a.dailyUsage);
                    }
                });

                const openBreakdownModal = (type) => {
                    breakdownType.value = type;
                    modals.breakdown = true;
                };

                const renderMarkdown = (text) => {
                    if (!text) return '';
                    return marked.parse(text, { breaks: true });
                };

                const toggleEditDesc = (item) => {
                    if (editingDesc[item.id]) {
                        editingDesc[item.id] = false;
                        syncData();
                    } else {
                        editingDesc[item.id] = true;
                    }
                };

                const toggleChart = async (item) => {
                    expandedCharts[item.id] = !expandedCharts[item.id];
                    if (expandedCharts[item.id]) {
                        await nextTick();
                        renderChart(item);
                    }
                };

                const switchChartMode = (item, mode) => {
                    chartMode[item.id] = mode;
                    renderChart(item);
                };

                const openItemModal = (item = null) => {
                    if (item) {
                        Object.assign(itemForm, JSON.parse(JSON.stringify(item)));
                        if (!itemForm.baseCategory) itemForm.baseCategory = 'productivity';
                    } else {
                        Object.assign(itemForm, { id: '', name: '', baseCategory: currentTab.value === 'all' ? 'productivity' : currentTab.value, purchaseDate: new Date().toISOString().split('T')[0], purchasePrice: '', warrantyMonths: '', status: 'active', description: '', image: '', receiptImage: '' });
                    }
                    modals.item = true;
                };

                const saveItem = () => {
                    if (!itemForm.name.trim()) return alert("资产名称为必填。");
                    const targetData = { ...itemForm, purchasePrice: Number(itemForm.purchasePrice) || 0, warrantyMonths: Number(itemForm.warrantyMonths) || 0 };
                    
                    if (targetData.id) {
                        const idx = items.value.findIndex(i => i.id === targetData.id);
                        if (idx !== -1) {
                            items.value[idx] = { ...items.value[idx], ...targetData };
                            const pEvent = items.value[idx].history.find(h => h.note === '购入价格' && h.type === 'price');
                            if (pEvent) {
                                pEvent.date = new Date(targetData.purchaseDate).toISOString();
                                pEvent.value = targetData.purchasePrice;
                                items.value[idx].history.sort((a, b) => new Date(b.date) - new Date(a.date));
                            }
                        }
                    } else {
                        items.value.unshift({ ...targetData, id: generateId('asset'), history: [] });
                        if (targetData.purchasePrice > 0 && targetData.purchaseDate) {
                            items.value[0].history.push({
                                id: generateId('h'), type: 'price', date: new Date(targetData.purchaseDate).toISOString(), value: targetData.purchasePrice, note: '购入价格'
                            });
                        }
                    }
                    modals.item = false;
                    syncData();
                    if (targetData.id && expandedCharts[targetData.id]) nextTick(() => renderChart(items.value.find(i=>i.id === targetData.id)));
                };

                const deleteItem = (id) => {
                    if (confirm("确认删除该记录？")) {
                        items.value = items.value.filter(i => i.id !== id);
                        if (chartInstances[id]) { chartInstances[id].dispose(); delete chartInstances[id]; }
                        syncData();
                    }
                };

                const openRecordModal = (item, type) => {
                    currentTargetItem.value = item;
                    const localNow = new Date(new Date().getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
                    recordForm.id = '';
                    recordForm.type = type;
                    recordForm.date = localNow;
                    recordForm.value = type === 'price' ? getLatestPrice(item, false) : '';
                    recordForm.note = '';
                    recordForm.cost = '';
                    recordForm.category = 'wear';
                    modals.record = true;
                };

                const editRecord = (item, event) => {
                    currentTargetItem.value = item;
                    const localDate = new Date(new Date(event.date).getTime() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
                    Object.assign(recordForm, {
                        id: event.id,
                        type: event.type,
                        date: localDate,
                        value: event.value,
                        note: event.note || '',
                        cost: event.cost || '',
                        category: event.category || 'wear'
                    });
                    modals.record = true;
                };

                const saveRecord = () => {
                    if (recordForm.value === '') return alert("内容不可为空。");
                    if (!currentTargetItem.value.history) currentTargetItem.value.history = [];
                    
                    const recordData = {
                        id: recordForm.id || generateId('h'), 
                        type: recordForm.type, 
                        date: new Date(recordForm.date).toISOString(),
                        value: recordForm.type === 'price' ? Number(recordForm.value) : recordForm.value,
                        note: recordForm.note,
                        cost: recordForm.type === 'event' && recordForm.cost ? Number(recordForm.cost) : undefined,
                        category: recordForm.type === 'event' ? recordForm.category : undefined
                    };

                    if (recordForm.id) {
                        const idx = currentTargetItem.value.history.findIndex(h => h.id === recordForm.id);
                        if (idx !== -1) currentTargetItem.value.history[idx] = recordData;
                    } else {
                        currentTargetItem.value.history.push(recordData);
                    }
                    
                    currentTargetItem.value.history.sort((a, b) => new Date(b.date) - new Date(a.date));
                    modals.record = false;
                    syncData();
                    
                    if (expandedCharts[currentTargetItem.value.id]) {
                        nextTick(() => renderChart(currentTargetItem.value));
                    }
                };

                const getCategoryStyle = (cat) => {
                    const map = {
                        wear: { text: '损耗', class: 'bg-orange-50 text-orange-600 border-orange-200' },
                        usage: { text: '使用', class: 'bg-sky-50 text-sky-600 border-sky-200' },
                        accessory: { text: '配件', class: 'bg-purple-50 text-purple-600 border-purple-200' },
                        accident: { text: '事故', class: 'bg-red-50 text-red-600 border-red-200' }
                    };
                    return map[cat] || map.wear;
                };

                const getEvents = (item) => {
                    return (item.history || []).filter(h => h.type === 'event').sort((a, b) => new Date(b.date) - new Date(a.date));
                };

                const getHardwareCost = (item) => {
                    const total = (item.history || []).reduce((sum, h) => {
                        const cat = h.category || 'wear'; 
                        if (h.type === 'event' && h.cost && (cat === 'wear' || cat === 'accessory')) return sum + Number(h.cost);
                        return sum;
                    }, 0);
                    return total.toLocaleString();
                };

                const getUsageCost = (item) => {
                    const total = (item.history || []).reduce((sum, h) => {
                        const cat = h.category || 'wear'; 
                        if (h.type === 'event' && h.cost && (cat === 'usage' || cat === 'accident')) return sum + Number(h.cost);
                        return sum;
                    }, 0);
                    return total.toLocaleString();
                };

                const calculateTCO = (item, formatted = true) => {
                    const currentVal = getLatestPrice(item, false);
                    if (typeof currentVal !== 'number' || !item.purchasePrice) return '-';
                    const hwCost = Number(getHardwareCost(item).replace(/,/g, ''));
                    const tco = item.purchasePrice + hwCost - currentVal;
                    return formatted ? tco.toLocaleString() : tco;
                };

                const getDaysHeld = (item) => {
                    if (!item.purchaseDate) return 1;
                    const priceRecords = (item.history || []).filter(h => h.type === 'price' && h.note !== '购入价格');
                    let latestDate = new Date(); 
                    if (priceRecords.length > 0) {
                        const latestPriceRecord = priceRecords.reduce((latest, current) => new Date(current.date) > new Date(latest.date) ? current : latest);
                        latestDate = new Date(latestPriceRecord.date);
                    }
                    return Math.max(1, Math.ceil((latestDate - new Date(item.purchaseDate)) / 86400000));
                };

                const getDailyTCO = (item) => {
                    const tcoStr = calculateTCO(item, false);
                    if (tcoStr === '-') return '-';
                    const days = getDaysHeld(item);
                    return (Number(tcoStr) / days).toFixed(2);
                };

                const getDailyUsage = (item) => {
                    if (!item.purchaseDate) return '-';
                    const usage = Number(getUsageCost(item).replace(/,/g, ''));
                    const days = getDaysHeld(item);
                    return (usage / days).toFixed(2);
                };

                const getPlateauStatus = (item) => {
                    const prices = (item.history || []).filter(h => h.type === 'price' && h.note !== '购入价格').sort((a, b) => new Date(a.date) - new Date(b.date));
                    if (item.purchaseDate && item.purchasePrice) {
                        prices.push({ date: new Date(item.purchaseDate).toISOString(), value: item.purchasePrice });
                        prices.sort((a, b) => new Date(a.date) - new Date(b.date));
                    }
                    if (prices.length < 3) return { text: '数据不足', color: 'text-slate-400' };
                    
                    const last = prices[prices.length - 1];
                    const prev = prices[prices.length - 2];
                    const oldest = prices[0];

                    const totalDays = Math.max(1, (new Date(last.date) - new Date(oldest.date)) / 86400000);
                    const totalDropRate = (oldest.value - last.value) / totalDays;

                    const recentDays = Math.max(1, (new Date(last.date) - new Date(prev.date)) / 86400000);
                    const recentDropRate = (prev.value - last.value) / recentDays;

                    if (recentDropRate <= 0) return { text: '逆势升值/横盘', color: 'text-emerald-500' };
                    if (recentDropRate < totalDropRate * 0.3) return { text: '平台期 (适宜出手)', color: 'text-blue-600' };
                    if (recentDropRate > totalDropRate * 1.5) return { text: '快速跳水期', color: 'text-red-500' };
                    return { text: '平稳贬值中', color: 'text-slate-600' };
                };

                const getWarrantyStatus = (item) => {
                    if (!item.purchaseDate || !item.warrantyMonths) return { text: '无', color: 'text-slate-400' };
                    const end = new Date(item.purchaseDate);
                    end.setMonth(end.getMonth() + item.warrantyMonths);
                    const diffDays = Math.ceil((end - new Date()) / 86400000);
                    
                    if (diffDays < 0) return { text: '已过保', color: 'text-slate-400' };
                    if (diffDays <= 30) return { text: `剩 ${diffDays} 天`, color: 'text-amber-500 font-semibold' };
                    return { text: `保修内`, color: 'text-emerald-500' };
                };

                const compressAndRead = (file, targetField) => {
                    new window.Compressor(file, {
                        quality: 0.7, maxWidth: 1200,
                        success(result) {
                            const reader = new FileReader();
                            reader.onload = (e) => itemForm[targetField] = e.target.result;
                            reader.readAsDataURL(result);
                        }
                    });
                };

                const handleImageUpload = (e, field) => {
                    if (e.target.files[0]) compressAndRead(e.target.files[0], field);
                    e.target.value = '';
                };

                const handlePaste = (e) => {
                    const items = (e.clipboardData || e.originalEvent.clipboardData).items;
                    for (let index in items) {
                        const item = items[index];
                        if (item.kind === 'file' && item.type.includes('image/')) {
                            const targetField = (!itemForm.image || document.activeElement.tagName === 'BODY') ? 'image' : 'receiptImage';
                            compressAndRead(item.getAsFile(), targetField);
                            e.preventDefault();
                            break;
                        }
                    }
                };

                const zoomImage = (src) => { zoomedImageSrc.value = src; };

                const hasEnoughChartData = (item) => {
                    const count = (item.history || []).filter(h => h.type === 'price' && h.note !== '购入价格').length;
                    return (count + (item.purchaseDate && item.purchasePrice ? 1 : 0)) >= 2;
                };

                const renderChart = (item) => {
                    const dom = chartsRefs[item.id];
                    if (!dom) return;

                    let chart = chartInstances[item.id];
                    if (!chart) {
                        chart = echarts.init(dom);
                        chartInstances[item.id] = chart;
                    }

                    if (!hasEnoughChartData(item)) {
                        chart.clear();
                        return;
                    }

                    const dateMap = new Map();
                    if (item.purchaseDate && item.purchasePrice) {
                        dateMap.set(new Date(item.purchaseDate).toISOString().split('T')[0], { timestamp: new Date(item.purchaseDate).getTime(), value: item.purchasePrice });
                    }
                    (item.history || []).filter(h => h.type === 'price' && h.note !== '购入价格').forEach(h => {
                        const dtStr = new Date(h.date).toISOString().split('T')[0];
                        const ts = new Date(h.date).getTime();
                        if (!dateMap.has(dtStr) || ts > dateMap.get(dtStr).timestamp) {
                            dateMap.set(dtStr, { timestamp: ts, value: h.value });
                        }
                    });
                    
                    let priceData = Array.from(dateMap.values()).sort((a, b) => a.timestamp - b.timestamp);

                    if (priceData.length > 0) {
                        const lastTs = priceData[priceData.length - 1].timestamp;
                        let maxEventTs = lastTs;
                        (item.history || []).forEach(h => {
                            const hTs = new Date(h.date).getTime();
                            if (hTs > maxEventTs) maxEventTs = hTs;
                        });
                        if (maxEventTs > lastTs + 86400000) { 
                            priceData.push({ timestamp: maxEventTs, value: priceData[priceData.length - 1].value });
                        }
                    }

                    const mode = chartMode[item.id] || 'net';
                    let seriesData = [];
                    let chartColor = mode === 'net' ? '#3b82f6' : '#ef4444';
                    let seriesName = mode === 'net' ? '净值' : '硬件TCO';

                    if (mode === 'net') {
                        seriesData = priceData.map(d => [d.timestamp, d.value]);
                    } else {
                        seriesData = priceData.map(p => {
                            const cumulativeHwCost = (item.history || [])
                                .filter(h => {
                                    const cat = h.category || 'wear';
                                    return h.type === 'event' && h.cost && (cat === 'wear' || cat === 'accessory') && new Date(h.date).getTime() <= p.timestamp;
                                })
                                .reduce((sum, h) => sum + Number(h.cost), 0);
                            return [p.timestamp, (item.purchasePrice || 0) + cumulativeHwCost - p.value];
                        });
                    }

                    chart.setOption({
                        grid: { top: 10, bottom: 20, left: 40, right: 10 },
                        tooltip: { 
                            trigger: 'axis',
                            formatter: function(params) {
                                const dt = new Date(params[0].value[0]);
                                const dateStr = `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
                                return `${dateStr}<br/>${seriesName}: ¥${params[0].value[1].toLocaleString()}`;
                            }
                        },
                        xAxis: {
                            type: 'time',
                            axisLine: { lineStyle: { color: '#e2e8f0' } },
                            axisLabel: { color: '#94a3b8', fontSize: 10, formatter: '{yyyy}-{MM}' },
                            splitLine: { show: false }
                        },
                        yAxis: { 
                            type: 'value', min: 'dataMin', 
                            splitLine: { lineStyle: { type: 'dashed', color: '#f1f5f9' } },
                            axisLabel: { color: '#94a3b8', fontSize: 10 }
                        },
                        series: [{
                            data: seriesData,
                            type: 'line', 
                            smooth: false, 
                            symbol: 'circle', symbolSize: 6,
                            itemStyle: { color: chartColor }, lineStyle: { color: chartColor, width: 2 },
                            areaStyle: {
                                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                                    { offset: 0, color: mode === 'net' ? 'rgba(59, 130, 246, 0.2)' : 'rgba(239, 68, 68, 0.2)' }, 
                                    { offset: 1, color: mode === 'net' ? 'rgba(59, 130, 246, 0)' : 'rgba(239, 68, 68, 0)' }
                                ])
                            }
                        }]
                    });
                };

                window.addEventListener('resize', () => {
                    Object.values(chartInstances).forEach(chart => chart.resize());
                });

                const getStatusColor = (status) => {
                    const map = { active: 'bg-emerald-50 text-emerald-600 border-emerald-200', idle: 'bg-slate-50 text-slate-500 border-slate-200', repairing: 'bg-amber-50 text-amber-600 border-amber-200', sold: 'bg-red-50 text-red-500 border-red-200' };
                    return map[status] || map.active;
                };
                const getStatusText = (status) => {
                    const map = { active: '在用', idle: '吃灰', repairing: '维修中', sold: '已出二手' };
                    return map[status] || '未知';
                };
                const formatDate = (dateStr) => {
                    if (!dateStr) return '';
                    const d = new Date(dateStr);
                    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
                };
                const guessEmoji = (name) => {
                    if(/相机|镜头|微单|索尼|佳能|尼康/.test(name)) return '📷';
                    if(/车|摩托|自行车|头盔|豪爵/.test(name)) return '🛵';
                    if(/手机|电脑|平板|Mac|iPhone/.test(name)) return '💻';
                    if(/表|劳力士|Apple Watch/.test(name)) return '⌚';
                    if(/吸尘器|冰箱|洗衣机|戴森/.test(name)) return '🔌';
                    return '📦';
                };

                const exportCSVView = () => {
                    let csvContent = '\uFEFF'; 
                    csvContent += '资产名称,所属分类,状态,购入日期,买入价格(¥),当前估价(¥),终生TCO(¥),近一年TCO流血(¥),近一年日均TCO(¥),近一年运行开销(¥),近一年日均运行(¥)\n';
                    
                    filteredItems.value.forEach(item => {
                        const name = `"${item.name.replace(/"/g, '""')}"`;
                        const cat = tabs.find(t => t.id === item.baseCategory)?.name || item.baseCategory;
                        const status = getStatusText(item.status);
                        const latestPrice = getLatestPrice(item, false);
                        const lifetimeTCO = calculateTCO(item, false);
                        const ttm = getAssetTtmStats(item);
                        
                        csvContent += `${name},${cat},${status},${item.purchaseDate || '-'},${item.purchasePrice || 0},${latestPrice || 0},${lifetimeTCO !== '-' ? lifetimeTCO : 0},${ttm.tco.toFixed(2)},${ttm.dailyTco.toFixed(2)},${ttm.usage.toFixed(2)},${ttm.dailyUsage.toFixed(2)}\n`;
                    });

                    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
                    const a = document.createElement('a');
                    a.href = URL.createObjectURL(blob);
                    a.download = `资产宏观报表_${new Date().toLocaleDateString()}.csv`;
                    a.click();
                };

                const exportJSONBackup = () => {
                    const dataStr = JSON.stringify(items.value, null, 2);
                    const blob = new Blob([dataStr], { type: 'application/json;charset=utf-8;' });
                    const a = document.createElement('a');
                    a.href = URL.createObjectURL(blob); 
                    a.download = `资产系统备份_${new Date().toLocaleDateString()}.json`;
                    a.click();
                };

                const importJSON = (e) => {
                    const file = e.target.files[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = (event) => {
                        try {
                            const parsed = JSON.parse(event.target.result);
                            if (Array.isArray(parsed) && confirm(`覆盖当前数据，确认导入？`)) {
                                items.value = parsed.map(i => {
                                    if (i.history && i.purchaseDate) {
                                        i.history.forEach(h => {
                                            if (h.type === 'price' && h.note === '购入价格') {
                                                h.date = new Date(i.purchaseDate).toISOString();
                                            }
                                        });
                                        i.history.sort((a, b) => new Date(b.date) - new Date(a.date));
                                    }
                                    return { ...i, baseCategory: i.baseCategory || 'productivity' };
                                });
                                syncData();
                            }
                        } catch (err) { alert("导入失败：文件格式错误。"); }
                        e.target.value = '';
                    };
                    reader.readAsText(file);
                };

                onMounted(loadData);

                return {
                    items, modals, itemForm, recordForm, currentTargetItem, chartsRefs, zoomedImageSrc, chartMode,
                    tabs, currentTab, filteredItems, globalStats, getCategoryCount, breakdownType, breakdownList,
                    editingDesc, expandedCharts, toggleEditDesc, toggleChart, switchChartMode, hasEnoughChartData,
                    openItemModal, saveItem, deleteItem, openRecordModal, editRecord, saveRecord, openBreakdownModal, getAssetTtmStats,
                    getLatestPrice, getEvents, getHardwareCost, getUsageCost, calculateTCO, getDailyTCO, getDailyUsage, getPlateauStatus, getWarrantyStatus, renderMarkdown, getCategoryStyle,
                    handleImageUpload, handlePaste, zoomImage, exportCSVView, exportJSONBackup, importJSON,
                    getStatusColor, getStatusText, formatDate, guessEmoji
                };
            }
        }).mount('#app');
    </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'cloud': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>订阅与虚拟资产看板</title>
    <script src="static/cdn/tailwindcss3.4.17.js"></script>
    <script src="static/cdn/vue3.5.global.prod.js"></script>
    <script src="static/cdn/compressor.min.js"></script>
    <style> 
        [v-cloak] { display: none; } 
        .custom-scrollbar::-webkit-scrollbar { width: 4px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background-color: #e5e7eb; border-radius: 4px; }
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
<body class="bg-gray-50 text-gray-800 p-4 md:p-6 font-sans">
    <div id="app" class="mx-auto" style="max-width: 900px;" v-cloak>
        <header class="flex flex-col md:flex-row justify-between items-center mb-6 gap-4">
            <h2 class="text-2xl font-bold flex items-center gap-2">🔄 订阅与虚拟资产看板</h2>
            <div class="flex gap-2">
                <button @click="openItemModal()" class="px-4 py-2 bg-blue-500 hover:bg-blue-600 text-white font-bold rounded-lg shadow-sm text-sm">+ 新建资产项</button>
            </div>
        </header>

        <div v-if="items.length === 0" class="text-center text-gray-400 py-16 border-2 border-dashed border-gray-200 rounded-xl">
            暂无追踪项目，请点击右上角新建。
        </div>
        
        <div class="grid gap-5" style="grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));">
            <div v-for="item in items" :key="item.id" :class="getCardClass(item)">
                
                <div v-if="item.type === 'sub' && item.autoRenew" class="absolute top-0 right-0 bg-green-100 text-green-700 text-[10px] px-2 py-1 rounded-bl-lg font-bold">
                    ♻️ 自动续费
                </div>

                <div class="flex justify-between items-start gap-2">
                    <div class="flex items-center gap-3 min-w-0">
                        <img v-if="item.image" :src="item.image" class="w-12 h-12 rounded-xl object-cover bg-white shrink-0 border">
                        <div v-else class="w-12 h-12 rounded-xl bg-white border flex items-center justify-center text-2xl shrink-0">{{ guessEmoji(item.name, item.type) }}</div>
                        <div class="flex-grow min-w-0">
                            <h3 class="font-bold text-lg truncate">{{ item.name }}</h3>
                            <span class="text-xs text-gray-500">{{ item.type === 'sub' ? '周期订阅' : '额度消耗 (余额扣减)' }}</span>
                        </div>
                    </div>
                    
                    <button @click="duplicateItem(item)" class="p-1.5 text-gray-400 hover:text-green-600 hover:bg-green-50 rounded-lg transition-colors shrink-0 mt-1 mr-1" title="以当前模板复制新建">
                        <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7v8a2 2 0 002 2h6M8 7V5a2 2 0 012-2h4.586a1 1 0 01.707.293l4.414 4.414a1 1 0 01.293.707V15a2 2 0 01-2 2h-2M8 7H6a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2v-2"></path></svg>
                    </button>
                </div>

                <div v-if="item.type === 'sub'" class="bg-white/50 rounded-xl p-3 border border-gray-100 mt-1">
                    <div class="flex justify-between items-end mb-2">
                        <div class="text-sm text-gray-500">距离到期还有</div>
                        <div class="text-2xl font-black" :class="getDaysLeft(item.expireDate) <= (item.reminderDays || 7) ? 'text-red-500' : 'text-gray-800'">
                            {{ getDaysLeft(item.expireDate) }} <span class="text-xs font-normal text-gray-500">天</span>
                        </div>
                    </div>
                    <div class="flex justify-between text-xs border-t border-gray-200/60 pt-2 mt-2">
                        <span class="text-gray-500">到期: {{ formatDateOnly(item.expireDate) }}</span>
                        <span class="font-bold text-gray-700">
                            <template v-if="item.isFree || item.cost === 0">免费 / {{ item.durationValue }}{{ item.durationUnit }}</template>
                            <template v-else>¥{{ item.cost }} / {{ item.durationValue }}{{ item.durationUnit }}</template>
                        </span>
                    </div>
                </div>

                <div v-if="item.type === 'quota'" class="bg-white/50 rounded-xl p-3 border border-gray-100 mt-1">
                    <div class="flex justify-between items-end mb-2">
                        <div class="text-sm text-gray-500">剩余可用余额</div>
                        <div class="text-2xl font-black" :class="item.balance <= (item.reminderQuota || item.autoDeductAmount * 2) ? 'text-orange-500' : 'text-gray-800'">
                            {{ Number(item.balance).toFixed(2).replace(/\.00$/, '') }} <span class="text-xs font-normal text-gray-500">{{ item.unit }}</span>
                        </div>
                    </div>
                    <div class="flex justify-between text-xs border-t border-gray-200/60 pt-2 mt-2">
                        <span class="text-gray-500">周期扣减: {{ item.autoDeductAmount || 0 }} {{ item.unit }}</span>
                        <span v-if="item.resetDay" class="font-bold text-red-500">每月 {{ item.resetDay }} 日自动扣款</span>
                    </div>
                </div>

                <!-- <div class="flex-grow">
                    <div class="text-[11px] text-gray-400 mb-1 border-b border-gray-100 pb-1">最近操作记录</div>
                    <div v-if="item.history && item.history.length" class="space-y-1 mt-2 max-h-24 overflow-y-auto custom-scrollbar text-xs">
                        <div v-for="rec in item.history.slice(0, 5)" :key="rec.date + Math.random()" class="flex justify-between text-gray-600">
                            <span>{{ formatDateOnly(rec.date) }}</span>
                            <span v-if="item.type === 'sub'" class="text-green-600">
                                {{ item.isFree ? '打卡/续期' : '续费 ¥' + (rec.cost ?? item.cost) }}
                            </span>
                            <span v-if="item.type === 'quota'" :class="rec.note === 'auto_deduct' ? 'text-red-500 font-bold' : 'text-blue-600'">
                                {{ rec.note === 'auto_deduct' ? `系统扣款: -${rec.cost}` : '手动调整余额为' }}: {{ rec.balance }} {{ item.unit }}
                            </span>
                        </div>
                    </div>
                    <div v-else class="text-xs text-gray-300 py-1">暂无记录</div>
                </div> -->

                <div class="flex justify-end gap-2 pt-3 border-t border-gray-100 mt-auto relative z-10 flex-wrap">
                    <button @click="openRecordModal(item)" class="px-3 py-1.5 bg-blue-50 hover:bg-blue-100 text-blue-600 font-bold rounded text-xs flex-grow text-center">
                        {{ item.type === 'sub' ? '📝 登记续期' : '➕ 补充余额/手动扣减' }}
                    </button>
                    <button @click="openItemModal(item)" class="px-3 py-1.5 bg-gray-100 hover:bg-gray-200 rounded text-xs">编辑配置</button>
                    <button @click="deleteItem(item.id)" class="px-3 py-1.5 bg-red-50 hover:bg-red-100 text-red-500 rounded text-xs">删除</button>
                </div>
            </div>
        </div>

        <div v-if="modals.item" class="fixed inset-0 bg-black/50 flex justify-center items-center z-50 p-4">
            <div class="bg-white rounded-2xl p-6 w-full max-w-lg shadow-xl max-h-[90vh] overflow-y-auto">
                <h3 class="text-lg font-bold mb-4">{{ itemForm.id ? '编辑资产项目' : '新建资产项目' }}</h3>
                <div class="space-y-4">
                    <div v-if="!itemForm.id" class="flex gap-2 bg-gray-100 p-1 rounded-lg">
                        <button @click="itemForm.type = 'sub'" :class="itemForm.type === 'sub' ? 'bg-white shadow text-black' : 'text-gray-500'" class="flex-1 py-1.5 rounded text-sm font-bold transition-all">📅 周期订阅</button>
                        <button @click="itemForm.type = 'quota'" :class="itemForm.type === 'quota' ? 'bg-white shadow text-black' : 'text-gray-500'" class="flex-1 py-1.5 rounded text-sm font-bold transition-all">🗄️ 额度预存扣费</button>
                    </div>

                    <div>
                        <label class="block text-sm font-medium mb-1">名称</label>
                        <input v-model="itemForm.name" type="text" placeholder="如: 电信20 / 阿里云服务器" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl focus:ring-2 focus:ring-blue-200 outline-none">
                    </div>

                    <template v-if="itemForm.type === 'sub'">
                        <div class="grid grid-cols-2 gap-3">
                            <div class="col-span-2 md:col-span-1">
                                <label class="block text-sm font-medium mb-1 text-blue-600">订阅开始日</label>
                                <input v-model="itemForm.startDate" type="date" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>
                            
                            <div class="col-span-2 md:col-span-1">
                                <label class="block text-sm font-medium mb-1 text-blue-600">订阅周期长度</label>
                                <div class="flex gap-2">
                                    <input v-model.number="itemForm.durationValue" type="number" min="1" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                                    <select v-model="itemForm.durationUnit" class="w-24 p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                                        <option value="天">天</option>
                                        <option value="月">个月</option>
                                        <option value="年">年</option>
                                    </select>
                                </div>
                            </div>

                            <div class="col-span-2 p-3 bg-blue-50/50 rounded-xl text-sm flex justify-between items-center border border-blue-100">
                                <span class="text-gray-600">预计到期日推算：</span>
                                <span class="font-bold text-blue-600 text-base">{{ calculateExpireDate(itemForm.startDate, itemForm.durationValue, itemForm.durationUnit) || '-' }}</span>
                            </div>

                            <div>
                                <label class="block text-sm font-medium mb-1">提醒时间段(剩余天数)</label>
                                <input v-model.number="itemForm.reminderDays" type="number" placeholder="默认 7 天" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>
                            
                            <div v-if="!itemForm.isFree">
                                <label class="block text-sm font-medium mb-1">单次扣费金额 (¥)</label>
                                <input v-model.number="itemForm.cost" type="number" step="0.1" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>

                            <div class="col-span-2 flex items-center gap-4 mt-2">
                                <label class="flex items-center gap-2 cursor-pointer text-sm font-medium">
                                    <input type="checkbox" v-model="itemForm.isFree" class="w-4 h-4 text-blue-600 rounded">
                                    免费/0扣费类型
                                </label>
                                <label class="flex items-center gap-2 cursor-pointer text-sm font-medium">
                                    <input type="checkbox" v-model="itemForm.autoRenew" class="w-4 h-4 text-green-600 rounded">
                                    标记为自动续费
                                </label>
                            </div>
                        </div>
                    </template>

                    <template v-if="itemForm.type === 'quota'">
                        <div class="grid grid-cols-2 gap-3">
                            <div class="col-span-2 md:col-span-1">
                                <label class="block text-sm font-medium mb-1 text-blue-600">建立时/当前剩余金额</label>
                                <input v-model.number="itemForm.balance" type="number" step="any" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none font-bold text-lg">
                            </div>
                            <div class="col-span-2 md:col-span-1">
                                <label class="block text-sm font-medium mb-1 text-blue-600">周期固定扣减量</label>
                                <input v-model.number="itemForm.autoDeductAmount" type="number" step="any" placeholder="每月自动减掉的值" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>

                            <div>
                                <label class="block text-sm font-medium mb-1">计量单位</label>
                                <input v-model="itemForm.unit" type="text" placeholder="如: ￥, GB, 次" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>
                            <div>
                                <label class="block text-sm font-medium mb-1">变色预警阈值</label>
                                <input v-model.number="itemForm.reminderQuota" type="number" step="any" :placeholder="`默认低于 ${itemForm.autoDeductAmount * 2 || '扣两次的值'} 飘黄`" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>

                            <div class="col-span-2 md:col-span-1">
                                <label class="block text-sm font-medium mb-1">起始/建档日期</label>
                                <input v-model="itemForm.startDate" type="date" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>
                            <div class="col-span-2 md:col-span-1">
                                <label class="block text-sm font-medium mb-1">每月自动扣费日(可选)</label>
                                <input v-model.number="itemForm.resetDay" type="number" min="1" max="31" placeholder="填 1-31，留空则仅手动操作" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                            </div>
                        </div>
                    </template>
                </div>
                <div class="flex justify-end gap-3 mt-6 pt-4 border-t border-gray-100">
                    <button @click="modals.item = false" class="px-4 py-2 bg-gray-100 rounded-xl text-sm font-bold text-gray-600">取消</button>
                    <button @click="saveItem" class="px-4 py-2 bg-black text-white rounded-xl text-sm font-bold">保存配置</button>
                </div>
            </div>
        </div>

        <div v-if="modals.record" class="fixed inset-0 bg-black/50 flex justify-center items-center z-50 p-4">
            <div class="bg-white rounded-2xl p-6 w-full max-w-md shadow-xl">
                <h3 class="text-lg font-bold mb-4">{{ currentTargetItem.type === 'sub' ? '登记续期 - ' : '余额调整 - ' }}{{ currentTargetItem.name }}</h3>
                
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium mb-1">操作日期</label>
                        <input v-model="recordForm.date" type="date" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                    </div>

                    <template v-if="currentTargetItem.type === 'sub'">
                        <div>
                            <label class="block text-sm font-medium mb-1 text-blue-600">本次续费时长</label>
                            <div class="flex gap-2">
                                <input v-model.number="recordForm.durationValue" type="number" min="1" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                                <select v-model="recordForm.durationUnit" class="w-24 p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                                    <option value="天">天</option>
                                    <option value="月">个月</option>
                                    <option value="年">年</option>
                                </select>
                            </div>
                        </div>

                        <div v-if="!currentTargetItem.isFree">
                            <label class="block text-sm font-medium mb-1">实际支付金额 (¥)</label>
                            <input v-model.number="recordForm.cost" type="number" step="0.1" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none">
                        </div>

                        <div class="p-3 bg-blue-50/50 rounded-xl text-sm flex justify-between items-center border border-blue-100 mt-2">
                            <span class="text-gray-600">续期后到期日：</span>
                            <span class="font-bold text-blue-600 text-base">{{ calculateExpireDate(currentTargetItem.expireDate, recordForm.durationValue, recordForm.durationUnit) || '-' }}</span>
                        </div>
                    </template>

                    <template v-if="currentTargetItem.type === 'quota'">
                        <div>
                            <label class="block text-sm font-medium mb-1 flex justify-between">
                                <span class="text-blue-600">调整后的最新总余额 ({{ currentTargetItem.unit }})</span>
                            </label>
                            <div class="flex gap-2">
                                <input v-model.number="recordForm.balance" type="number" step="any" class="w-full p-2 bg-gray-50 border border-gray-200 rounded-xl outline-none font-bold text-lg">
                                <button v-if="currentTargetItem.autoDeductAmount > 0" 
                                        @click="applyAutoDeduct"
                                        class="px-4 bg-orange-100 text-orange-600 hover:bg-orange-200 font-bold rounded-xl whitespace-nowrap">
                                    一键扣除单次 (-{{ currentTargetItem.autoDeductAmount }})
                                </button>
                            </div>
                            <div class="text-xs text-gray-400 mt-2">提示：若在此处充值，请直接修改为充值后的最终金额。</div>
                        </div>
                    </template>
                </div>

                <div class="flex justify-end gap-3 mt-6 pt-4 border-t border-gray-100">
                    <button @click="modals.record = false" class="px-4 py-2 bg-gray-100 rounded-xl text-sm font-bold text-gray-600">取消</button>
                    <button @click="saveRecord" class="px-4 py-2 bg-blue-500 text-white rounded-xl text-sm font-bold">确认提交</button>
                </div>
            </div>
        </div>

    </div>

    <script>
        const { createApp, ref, reactive, onMounted } = Vue;

        createApp({
            setup() {
                const API_URL = API_BASE + '/tracker_data/sync/daily';
                const items = ref([]);
                const modals = reactive({ item: false, record: false });
                const currentTargetItem = ref(null);
                
                const getTodayStr = () => {
                    const d = new Date();
                    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
                };

                const itemForm = reactive({ 
                    id: '', name: '', type: 'sub', image: '',
                    startDate: getTodayStr(), durationValue: 1, durationUnit: '月', expireDate: '', cost: 0, 
                    autoRenew: true, reminderDays: 7, isFree: false,
                    balance: 10, unit: '次', resetDay: null, lastDeductDate: '',
                    reminderQuota: null, autoDeductAmount: 1
                });

                const recordForm = reactive({
                    date: '', cost: 0, balance: 0, durationValue: 1, durationUnit: '月'
                });

                const generateId = (prefix) => prefix + '_' + Date.now() + '_' + Math.random().toString(36).substr(2, 5);

                const loadData = async () => {
                    try {
                        const res = await fetch(API_URL);
                        if (res.ok) {
                            const data = await res.json();
                            items.value = data[0]?.items || [];
                            // 加载后立刻执行自动扣款检查
                            checkAndApplyDeductions();
                        } else {
                            throw new Error("加载失败");
                        }
                    } catch (err) {
                        console.warn("API 失败，使用本地空数据");
                    }
                };

                const syncData = async () => {
                    try {
                        await fetch(API_URL, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ items: items.value })
                        });
                    } catch (e) { 
                        console.error("同步失败"); 
                    }
                };

                // --- 核心修正：时间轴追踪，补齐漏掉的历史扣费记录 ---
                const checkAndApplyDeductions = () => {
                    let needsSync = false;
                    const today = new Date();
                    today.setHours(23, 59, 59, 999);

                    items.value.forEach(item => {
                        // 必须是 quota 模式，且设置了每月扣费日和扣费金额
                        if (item.type === 'quota' && item.resetDay && item.autoDeductAmount > 0) {
                            // 若 JSON 中没有记录上一次扣费时间，则以 startDate 为锚点
                            let startBase = item.lastDeductDate || item.startDate;
                            if (!startBase) return;

                            let [y, m, d] = startBase.split('-');
                            let cursorDate = new Date(y, m - 1, d);
                            
                            let nextMonth = cursorDate.getMonth();
                            let nextYear = cursorDate.getFullYear();
                            
                            // 确定下一个有效扣费日：如果起始日已经大于等于扣费日，则推到下个月
                            if (cursorDate.getDate() >= item.resetDay) {
                                nextMonth++;
                                if (nextMonth > 11) { nextMonth = 0; nextYear++; }
                            }
                            
                            let targetDate = new Date(nextYear, nextMonth, item.resetDay);

                            // 当推算出的下一个扣款日期在今天或今天之前，并且余额充足，执行循环扣款
                            while (targetDate <= today && item.balance > 0) {
                                item.balance = Math.max(0, parseFloat((item.balance - item.autoDeductAmount).toFixed(4)));
                                
                                const dd = String(targetDate.getDate()).padStart(2, '0');
                                const mm = String(targetDate.getMonth() + 1).padStart(2, '0');
                                const yy = targetDate.getFullYear();
                                const dateStr = `${yy}-${mm}-${dd}`;
                                
                                if (!item.history) item.history = [];
                                item.history.unshift({
                                    date: dateStr,
                                    balance: item.balance,
                                    note: 'auto_deduct',
                                    cost: item.autoDeductAmount
                                });

                                item.lastDeductDate = dateStr;
                                needsSync = true;

                                // 游标移向再下一个月
                                nextMonth++;
                                if (nextMonth > 11) { nextMonth = 0; nextYear++; }
                                targetDate = new Date(nextYear, nextMonth, item.resetDay);
                            }
                        }
                    });
                    
                    if (needsSync) syncData();
                };

                const calculateExpireDate = (startStr, val, unit) => {
                    if (!startStr || !val) return '';
                    const [y, m, d] = startStr.split('-');
                    const date = new Date(y, m - 1, d);
                    
                    if (unit === '年') date.setFullYear(date.getFullYear() + val);
                    else if (unit === '月') date.setMonth(date.getMonth() + val);
                    else if (unit === '天') date.setDate(date.getDate() + val);
                    
                    const yy = date.getFullYear();
                    const mm = String(date.getMonth() + 1).padStart(2, '0');
                    const dd = String(date.getDate()).padStart(2, '0');
                    return `${yy}-${mm}-${dd}`;
                };

                const getDaysLeft = (dateStr) => {
                    if (!dateStr) return 0;
                    const [y, m, d] = dateStr.split('-');
                    const target = new Date(y, m - 1, d);
                    const now = new Date();
                    now.setHours(0, 0, 0, 0);
                    return Math.ceil((target - now) / (1000 * 60 * 60 * 24));
                };

                const getCardClass = (item) => {
                    let base = 'rounded-2xl shadow-sm border p-5 flex flex-col gap-4 hover:shadow-md transition-all relative overflow-hidden ';
                    if (item.type === 'sub') {
                        const daysLeft = getDaysLeft(item.expireDate);
                        const reminder = item.reminderDays ?? 7;
                        if (daysLeft <= reminder) return base + 'bg-red-50 border-red-200';
                    } else if (item.type === 'quota') {
                        const limit = (item.reminderQuota !== null && item.reminderQuota !== '' && item.reminderQuota !== undefined) 
                            ? item.reminderQuota 
                            : (item.autoDeductAmount * 2);
                        if (item.balance <= limit) return base + 'bg-orange-50 border-orange-200';
                    }
                    return base + 'bg-white border-gray-100';
                };

                const openItemModal = (item = null) => {
                    if (item) {
                        Object.assign(itemForm, {
                            ...item,
                            durationValue: item.durationValue || 1,
                            durationUnit: item.durationUnit || '月',
                            startDate: item.startDate || getTodayStr() // 填补历史缺失
                        });
                    } else {
                        Object.assign(itemForm, { 
                            id: '', name: '', type: 'sub', image: '',
                            startDate: getTodayStr(), durationValue: 1, durationUnit: '月', expireDate: '', cost: 0, autoRenew: true, reminderDays: 7, isFree: false,
                            balance: 10, unit: '￥', resetDay: null, lastDeductDate: '', reminderQuota: null, autoDeductAmount: 1
                        });
                    }
                    modals.item = true;
                };

                const duplicateItem = (item) => {
                    const newItem = JSON.parse(JSON.stringify(item));
                    newItem.id = ''; 
                    newItem.name = newItem.name + ' (副本)';
                    newItem.history = []; 
                    newItem.startDate = getTodayStr(); 
                    if (newItem.type === 'quota') {
                        newItem.lastDeductDate = ''; // 副本清除旧的自动扣款时间戳
                    }
                    openItemModal(newItem);
                };

                const saveItem = () => {
                    if (!itemForm.name.trim()) return alert("名称不可为空！");
                    if (itemForm.isFree) itemForm.cost = 0;
                    
                    const payload = JSON.parse(JSON.stringify(itemForm));
                    payload.history = itemForm.history || [];
                    
                    if (payload.type === 'sub') {
                        payload.expireDate = calculateExpireDate(payload.startDate, payload.durationValue, payload.durationUnit);
                    }

                    if (itemForm.id) {
                        const idx = items.value.findIndex(i => i.id === itemForm.id);
                        if(idx > -1) items.value[idx] = payload;
                    } else {
                        payload.id = generateId('asset');
                        items.value.push(payload);
                    }
                    modals.item = false;
                    syncData();
                };

                const deleteItem = (id) => {
                    if (confirm("确认删除该资产及所有记录？")) {
                        items.value = items.value.filter(i => i.id !== id);
                        syncData();
                    }
                };

                const openRecordModal = (item) => {
                    currentTargetItem.value = item;
                    recordForm.date = getTodayStr();
                    if (item.type === 'sub') {
                        recordForm.cost = item.isFree ? 0 : item.cost;
                        recordForm.durationValue = item.durationValue || 1;
                        recordForm.durationUnit = item.durationUnit || '月';
                    } else {
                        recordForm.balance = item.balance || 0;
                    }
                    modals.record = true;
                };

                const applyAutoDeduct = () => {
                    const amount = currentTargetItem.value.autoDeductAmount || 0;
                    recordForm.balance = Math.max(0, parseFloat((recordForm.balance - amount).toFixed(4)));
                };

                const saveRecord = () => {
                    const target = items.value.find(i => i.id === currentTargetItem.value.id);
                    if (!target) return;

                    const newRecord = { date: recordForm.date };

                    if (target.type === 'sub') {
                        newRecord.cost = recordForm.cost;
                        target.expireDate = calculateExpireDate(target.expireDate, recordForm.durationValue, recordForm.durationUnit);
                    } else {
                        newRecord.balance = recordForm.balance;
                        target.balance = recordForm.balance;
                    }

                    if (!target.history) target.history = [];
                    target.history.unshift(newRecord);
                    
                    modals.record = false;
                    syncData();
                };

                const formatDateOnly = (dateStr) => dateStr ? dateStr.split('T')[0] : '-';

                const guessEmoji = (name, type) => {
                    const n = name.toLowerCase();
                    if(n.includes('netflix') || n.includes('爱奇艺') || n.includes('影视') || n.includes('视频')) return '🍿';
                    if(n.includes('流量') || n.includes('宽带') || n.includes('话费') || n.includes('移动') || n.includes('电信') || n.includes('联通')) return '📶';
                    if(n.includes('云') || n.includes('网盘') || n.includes('icloud') || n.includes('115')) return '☁️';
                    if(n.includes('健身') || n.includes('gym') || n.includes('跑')) return '🏋️';
                    if(n.includes('阅读') || n.includes('书') || n.includes('杂志')) return '📖';
                    if(n.includes('电') || n.includes('水') || n.includes('燃气')) return '⚡';
                    if(n.includes('gpt') || n.includes('ai') || n.includes('api')) return '🤖';
                    if(n.includes('车')) return '🚗';
                    return type === 'sub' ? '💳' : '📊';
                };

                onMounted(loadData);

                return {
                    items, modals, itemForm, recordForm, currentTargetItem,
                    openItemModal, duplicateItem, saveItem, deleteItem, openRecordModal, saveRecord, applyAutoDeduct, calculateExpireDate,
                    getDaysLeft, getCardClass, formatDateOnly, guessEmoji
                };
            }
        }).mount('#app');
    </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'exercise': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>自重与功能性训练记录</title>
  <script src="static/cdn/vue3.5.global.prod.js"></script>
  <style>
    body {
      background-color: #f0f4f8;
      min-height: 100vh;
      margin: 0;
      padding: 16px;
      padding-top: env(safe-area-inset-top);
      padding-bottom: env(safe-area-inset-bottom);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    .app-container {
      max-width: 600px;
      margin: 0 auto;
      box-sizing: border-box;
    }
    h1 {
      text-align: center;
      color: #1e40af;
      font-size: 1.5rem;
      font-weight: bold;
      margin-bottom: 24px;
    }

    .knowledge-card {
      background: white;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 20px;
      box-shadow: 0 4px 6px rgba(0,0,0,0.05);
    }
    .knowledge-header {
      font-weight: 700;
      color: #1e40af;
      margin-bottom: 12px;
      font-size: 16px;
      display: flex;
      align-items: center;
    }
    .knowledge-header i { margin-right: 6px; }

    .tags-container {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 12px;
    }
    .knowledge-tag {
      background: #f1f5f9;
      color: #475569;
      padding: 6px 12px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      border: 1px solid transparent;
    }
    .knowledge-tag.active {
      background: #eff6ff;
      color: #1d4ed8;
      border-color: #bfdbfe;
    }
    .knowledge-content {
      font-size: 14px;
      color: #334155;
      line-height: 1.6;
      white-space: pre-wrap;
      background: #f8fafc;
      padding: 12px;
      border-radius: 8px;
      border-left: 4px solid #3b82f6;
      min-height: 80px;
    }

    .plan-selector {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 20px;
    }
    .plan-btn {
      flex: 1 1 calc(33.333% - 8px);
      min-width: 90px;
      padding: 10px 4px;
      border: 2px solid #e2e8f0;
      border-radius: 12px;
      background: white;
      color: #64748b;
      font-weight: 600;
      font-size: 13px;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s;
    }
    .plan-btn.active {
      border-color: #1e40af;
      background: #1e40af;
      color: white;
      box-shadow: 0 4px 6px rgba(30, 64, 175, 0.2);
    }

    .exercise-item {
      margin-bottom: 16px;
      border-radius: 12px;
      padding: 16px;
      background: white;
      box-shadow: 0 4px 6px rgba(0,0,0,0.05);
      border: 1px solid #e2e8f0;
    }

    .exercise-label {
      font-size: 16px;
      font-weight: 600;
      color: #1e40af;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 8px;
    }
    .exercise-label-left {
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
    }
    .intensity-badge {
      padding: 2px 8px;
      background: #dbeafe;
      color: #1e40af;
      border-radius: 12px;
      font-size: 12px;
      white-space: nowrap;
    }
    .priority-badge {
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 700;
    }
    .priority-badge.must { background: #dcfce7; color: #166534; }
    .priority-badge.opt { background: #e0f2fe; color: #0369a1; }
    .priority-badge.strict { background: #fee2e2; color: #991b1b; }
    .priority-badge.flex { background: #f3f4f6; color: #475569; }
    
    .exercise-note {
      font-size: 12px;
      color: #64748b;
      margin-bottom: 12px;
      line-height: 1.4;
      background: #f8fafc;
      padding: 6px 10px;
      border-radius: 6px;
      border-left: 3px solid #cbd5e1;
    }

    .input-row {
      display: flex;
      align-items: flex-end;
      gap: 8px;
      justify-content: space-between;
      flex-wrap: wrap;
    }

    .input-group {
      display: flex;
      align-items: center;
      gap: 6px;
      flex: 1;
      flex-wrap: wrap;
    }
    .input-label {
      font-size: 12px;
      color: #64748b;
      text-align: center;
      margin-bottom: 4px;
    }
    .input-group input {
      padding: 10px 0;
      font-size: 15px;
      font-weight: bold;
      text-align: center;
      border: 1px solid #e2e8f0;
      border-radius: 10px;
      background: #f8fafc;
      width: 55px;
      box-sizing: border-box;
      transition: all 0.2s;
    }
    .input-group input:focus {
      outline: none;
      border-color: #1e40af;
      background: white;
      box-shadow: 0 0 0 3px rgba(30, 64, 175, 0.1);
    }
    .input-group span {
      font-size: 16px;
      color: #64748b;
      font-weight: bold;
      padding-top: 18px;
    }

    .btn-deload {
      padding: 10px 12px;
      background: #fef2f2;
      color: #ef4444;
      border: 1px solid #fca5a5;
      border-radius: 10px;
      font-weight: bold;
      font-size: 13px;
      cursor: pointer;
      height: 42px;
      box-sizing: border-box;
    }
    .btn-deload:active { background: #fee2e2; }

    .rating-container {
      display: flex;
      justify-content: center;
      gap: 8px;
      margin: 16px 0;
    }
    .rating-star {
      font-size: 2.5rem;
      cursor: pointer;
      color: #cbd5e1;
      transition: all 0.2s;
    }
    .rating-star.active { color: #f59e0b; }

    .btn-generate {
      width: 100%;
      padding: 16px;
      font-size: 18px;
      border-radius: 16px;
      background: linear-gradient(135deg, #1e40af, #0c4a6e);
      color: white;
      border: none;
      margin-top: 16px;
      box-shadow: 0 4px 12px rgba(30, 64, 175, 0.3);
      cursor: pointer;
      font-weight: bold;
    }

    .record-container {
      background: white;
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.08);
      margin-top: 24px;
      line-height: 1.6;
      font-size: 15px;
    }
    .record-content {
      white-space: pre-wrap;
      color: #4b5563;
      background: #f8fafc;
      padding: 12px;
      border-radius: 8px;
      border: 1px dashed #cbd5e1;
    }
    .copy-btn {
      width: 100%;
      padding: 12px;
      margin-top: 12px;
      border-radius: 12px;
      background: #e2e8f0;
      color: #334155;
      border: none;
      font-weight: 600;
      cursor: pointer;
    }

    .form-group input {
      padding: 12px 16px;
      font-size: 16px;
      border: 1px solid #e2e8f0;
      border-radius: 12px;
      background: #f8fafc;
      width: 100%;
      box-sizing: border-box;
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
  <div class="app-container" id="app">
    <h1>自重与功能性训练记录</h1>

    <div class="knowledge-card">
      <div class="knowledge-header">
        <i>💡</i> 核心原则与生理监控
      </div>
      <div class="tags-container">
        <span
          v-for="(item, index) in knowledgeDB"
          :key="index"
          @click="activeKnowledgeIndex = index"
          class="knowledge-tag"
          :class="{ active: activeKnowledgeIndex === index }"
        >
          {{ item.category }}
        </span>
      </div>
      <div class="knowledge-content">{{ currentKnowledge?.content }}</div>
    </div>

    <!-- 顶部计划切换区 -->
    <div class="plan-selector">
      <div
        v-for="key in planKeys"
        :key="key"
        class="plan-btn"
        :class="{ active: currentPlan === key }"
        @click="switchPlan(key)"
      >
        {{ plans[key].label }}
      </div>
    </div>

    <!-- 数据记录区 -->
    <div>
      <h2 style="font-size: 1.2rem; font-weight: bold; color: #1e40af; margin-bottom: 16px;">记录数据</h2>

      <div v-for="(exercise, index) in currentExercises" :key="exercise.name" class="exercise-item">
        <div class="exercise-label">
          <div class="exercise-label-left">
            <i style="margin-right:8px;">🎯</i>
            <span>{{ index + 1 }}. {{ exercise.name }}</span>
            <span v-if="exercise.priority === 'must'" class="priority-badge must">必做</span>
            <span v-if="exercise.priority === 'optional'" class="priority-badge opt">可选</span>
            <span v-if="exercise.order === 'strict'" class="priority-badge strict">严格优先</span>
            <span v-if="exercise.order === 'flexible'" class="priority-badge flex">灵活穿插</span>
          </div>
          <span class="intensity-badge">单次强度: {{ exercise.intensity }}</span>
        </div>
        
        <div v-if="exercise.note" class="exercise-note">
          <i style="margin-right:4px;">💡</i>{{ exercise.note }}
        </div>

        <div class="input-row" v-if="inputs[exercise.name]">
          <div class="input-group">
            <div v-if="inputs[exercise.name].weight !== undefined">
              <div class="input-label">负重(kg)</div>
              <input type="number" step="0.5" min="0" v-model.number="inputs[exercise.name].weight">
            </div>
            <span v-if="inputs[exercise.name].weight !== undefined && inputs[exercise.name].sets !== undefined">+</span>

            <div v-if="inputs[exercise.name].sets !== undefined">
              <div class="input-label">组数</div>
              <input type="number" min="0" v-model.number="inputs[exercise.name].sets">
            </div>
            <span v-if="inputs[exercise.name].reps !== undefined || inputs[exercise.name].time !== undefined">×</span>

            <div v-if="inputs[exercise.name].reps !== undefined">
              <div class="input-label">次数</div>
              <input type="number" min="0" v-model.number="inputs[exercise.name].reps">
            </div>

            <div v-if="inputs[exercise.name].time !== undefined">
              <div class="input-label">时间(s)</div>
              <input type="number" min="0" step="5" v-model.number="inputs[exercise.name].time">
            </div>
          </div>
          <button @click="deload(exercise)" class="btn-deload">降阶</button>
        </div>
      </div>
    </div>

    <!-- 综合评价区 -->
    <div class="exercise-item">
      <h2 style="font-size: 1.2rem; font-weight: bold; color: #1e40af; margin-bottom: 8px; margin-top:0;">体感评分 (综合疲劳度)</h2>
      <div class="rating-container">
        <span
          v-for="i in 5"
          :key="i"
          class="rating-star"
          :class="{ 'active': i <= rating }"
          @click="rating = i"
        >★</span>
      </div>
    </div>
    <div class="exercise-item">
      <div style="display: flex; gap: 12px; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 100px;" class="form-group">
          <label style="font-weight: bold; color: #1e40af; display: block; margin-bottom: 8px; font-size:14px;">用时 (分钟)</label>
          <input type="number" v-model.number="duration">
        </div>
        <div style="flex: 2; min-width: 200px;" class="form-group">
          <label style="font-weight: bold; color: #1e40af; display: block; margin-bottom: 8px; font-size:14px;">环境/状态备注</label>
          <input type="text" v-model="remark" placeholder="气温/代偿/DOMS反馈等">
        </div>
      </div>
    </div>

    <!-- ================= 核心：全自动换行批量修改模块 ================= -->
    <div class="exercise-item">
      <div class="exercise-label" @click="showBatchEdit = !showBatchEdit" style="cursor: pointer; margin-bottom: 0;">
        <div class="exercise-label-left">
          <i>📝</i> <span>快捷批量修改配置 (文字版)</span>
        </div>
      </div>
      <div v-show="showBatchEdit" style="margin-top: 12px;">
        
        <div class="exercise-note" style="background:#e0f2fe; border-left-color:#38bdf8;">
          💡 <b>提示：</b>下方的文本框会<b>实时自动同步</b>为您勾选的计划配置！直接在文字上修改数字（格式：动作名 属性=值），然后点击【应用修改】即可。<br>
          支持属性：重量, 组数, 次数, 时间, 强度。
        </div>

        <div style="margin-bottom: 8px; font-weight:bold; font-size: 13px; color: #1e40af;">请勾选计划（自动加载至文本框，支持多选）：</div>
        <!-- 优化样式：独立卡片避免粘连 -->
        <div style="display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; background: #f8fafc; padding: 10px; border-radius: 8px;">
          <label v-for="key in planKeys" :key="key" style="font-size: 13px; display: inline-flex; align-items: center; gap: 6px; cursor: pointer; padding: 6px 12px; background: white; border: 1px solid #cbd5e1; border-radius: 6px; box-shadow: 0 1px 2px rgba(0,0,0,0.05);">
            <input type="checkbox" :value="key" v-model="batchEditPlans" @change="loadSelectedPlansText" style="margin: 0;"> 
            <span style="font-weight: 600; color: #334155;">{{ key }}</span>
          </label>
        </div>

        <textarea v-model="batchEditText" style="width: 100%; height: 220px; padding: 12px; border: 1px solid #94a3b8; border-radius: 8px; font-size: 14px; box-sizing: border-box; outline: none; background: white; white-space: pre; font-family: monospace;"></textarea>
        
        <div style="display: flex; gap: 8px; margin-top: 8px;">
          <!-- 重新加载按钮保留，以防用户改乱了想恢复 -->
          <button @click="loadSelectedPlansText" class="copy-btn" style="flex: 1; margin-top: 0; background: #f1f5f9; color: #475569;">🔄 放弃修改 (重新加载)</button>
          <button @click="applyBatchEdit" class="copy-btn" style="flex: 2; margin-top: 0; background: #1e40af; color: white; font-size: 15px;">⚡ 立即应用修改</button>
        </div>
      </div>
    </div>
    <!-- ========================================================= -->

    <!-- 高级 JSON 与云端同步模块 -->
    <div class="exercise-item">
      <div class="exercise-label" @click="showConfig = !showConfig" style="cursor: pointer; margin-bottom: 0;">
        <div class="exercise-label-left">
          <i>⚙️</i> <span>高级 JSON 配置与云端备份</span>
        </div>
      </div>
      <div v-show="showConfig" style="margin-top: 12px;">
        <div class="exercise-note">在此进行云端备份/拉取。云端Key固定为：<b>gym_cfg</b></div>
        <textarea v-model="configJsonStr" style="width: 100%; height: 200px; padding: 10px; border: 1px solid #e2e8f0; border-radius: 8px; font-family: monospace; font-size: 12px; box-sizing: border-box; background: #f8fafc; white-space: pre; outline: none;"></textarea>
        <div style="display: flex; gap: 8px; margin-top: 12px;">
          <button @click="applyLocalConfig" class="copy-btn" style="flex: 1; margin-top:0; background:#dbeafe; color:#1e40af;">应用本地</button>
          <button @click="loadFromCloud" class="copy-btn" style="flex: 1; margin-top:0; background:#fef3c7; color:#b45309;">云端加载</button>
          <button @click="saveToCloud" class="copy-btn" style="flex: 1; margin-top:0; background:#dcfce7; color:#166534;">云端保存</button>
        </div>
      </div>
    </div>

    <!-- 底部生成记录功能 -->
    <button @click="generateRecord" class="btn-generate">一键生成复盘记录</button>

    <div v-if="generatedRecord" class="record-container">
      <div style="text-align: center; color: #1e40af; font-weight: bold; margin-bottom: 12px;">当前记录已生成</div>
      <div class="record-content">{{ generatedRecord }}</div>
      <button @click="copyRecord" class="copy-btn">复制到剪贴板</button>
    </div>
  </div>

  <script>
    const { createApp } = Vue;

const KNOWLEDGE_DB = [
  {
    category: "修补厂法则",
    content: `健身房不是主战场，是修补厂。骑行是引擎，体操是技能，徒步是享受。\nGym的唯一使命：\n修复骑行/体操/徒步覆盖不了的不均衡，防止未来出问题。\n\n核心原则：\n结构稳定 > 动作质量 > 重量数字。`
  },
  {
    category: "RDL髋铰链纪律",
    content: `【核心】\nRDL学的是髋铰链模式——弯腰时用髋关节而不是腰椎发力。\n【纪律】\n空杆练到完美，不急着加重。\n允许练不到，不允许腰代偿。`
  },
  {
    category: "极限重量引体/双杠纪律",
    content: `【负重引体：绝对控制】\n必须底端静止启动，严禁甩动借力。\n双腿并拢微向前伸（空心体），用大腿内侧夹稳铃片，消灭钟摆晃动。\n严禁向后交叉弯曲小腿（会导致骨盆前倾拉扯腰部）。`
  },
  {
    category: "预疲劳与真实容量",
    content: `【接受退步假象】\n高难度抗旋动作初期，极易引发下背DOMS并破坏骨盆稳定。\n【真实容量】\n严格保留2次余力。禁止凑数导致动作变形。`
  },
  {
    category: "恢复债务与结构适应",
    content: `【恢复债务】\n局部组织（肌腱/筋膜/深层稳定）恢复远慢于心肺与意志。\n腰不痛 ≠ 恢复完成。\n【危险阶段】\n感觉还能练、还能加重、但长期存在轻微紧张感时，最容易进入慢性代偿。`
  }
];

const PLANS = {
  "Gym_A": {
    "label": "Gym A: 力量突破 (主冲1RM)",
    "duration": 90,
    "exercises": [
      { "name": "哑铃抓举", "weight": 30, "sets": 3, "reps": 3, "intensity": 8, "order": "strict", "note": "激活项。发力必须绝对垂直。" },
      { "name": "负重引体向上", "weight": "极限", "sets": 5, "reps": 1, "intensity": 10, "order": "strict", "note": "主项一。冲击1RM。需底端静止启动消灭钟摆。" },
      { "name": "负重双杠臂屈伸", "weight": 40, "sets": 4, "reps": 3, "intensity": 9, "order": "strict", "note": "主项二。3RM积累，严禁做单次极限。" },
      { "name": "泽奇深蹲", "weight": 80, "sets": 3, "reps": 5, "intensity": 9, "order": "flexible", "note": "主项三。零腰部剪切力，强化前侧核心。" },
      { "name": "双手农夫行走", "weight": 60, "sets": 3, "time": 40, "intensity": 8, "order": "flexible", "note": "严禁单手，必须对称负重防骨盆旋转。" },
      { "name": "杠铃推肩", "weight": 40, "sets": 3, "reps": 5, "intensity": 7, "order": "flexible", "note": "见缝插针，禁止做到力竭抢占双杠恢复配额。" },
      { "name": "器械单手划船", "weight": 70, "sets": 3, "reps": 5, "intensity": 7, "order": "flexible", "note": "器械空出随时可做。" },
      { "name": "面拉", "weight": 30, "sets": 3, "reps": 12, "intensity": 5, "order": "flexible", "note": "康复收尾。固定在最后进行。" }
    ]
  },
  "Gym_B": {
    "label": "Gym B: 容量增厚 (A日容量版)",
    "duration": 90,
    "exercises": [
      { "name": "哑铃抓举", "weight": 20, "sets": 4, "reps": 5, "intensity": 6, "order": "strict" },
      { "name": "负重引体向上/高位下拉", "weight": 10, "sets": 4, "reps": 10, "intensity": 8, "order": "strict" },
      { "name": "负重双杠臂屈伸", "weight": 20, "sets": 4, "reps": 10, "intensity": 8, "order": "strict" },
      { "name": "泽奇深蹲", "weight": 60, "sets": 3, "reps": 10, "intensity": 8, "order": "flexible" },
      { "name": "双手农夫行走", "weight": 50, "sets": 3, "time": 45, "intensity": 7, "order": "flexible" },
      { "name": "杠铃推肩", "weight": 30, "sets": 4, "reps": 10, "intensity": 7, "order": "flexible" },
      { "name": "器械单手划船", "weight": 50, "sets": 4, "reps": 10, "intensity": 7, "order": "flexible" },
      { "name": "俯卧腿弯举", "weight": 30, "sets": 3, "reps": 12, "intensity": 6, "order": "flexible" },
      { "name": "坐姿腿伸展", "weight": 40, "sets": 3, "reps": 12, "intensity": 6, "order": "flexible" },
      { "name": "面拉", "weight": 25, "sets": 3, "reps": 15, "intensity": 5, "order": "flexible" },
      { "name": "单手悬挂", "weight": 0, "sets": 3, "time": 15, "intensity": 4, "order": "flexible" }
    ]
  },
  "Gym_Base": {
    "label": "Gym: 综合一周",
    "duration": 160,
    "exercises": [
      { "name": "哑铃抓举", "weight": 26, "sets": 3, "reps": 5, "intensity": 8 },
      { "name": "腰间俯卧撑进阶俄挺", "sets": 4, "reps": 5, "intensity": 9 },
      { "name": "保加利亚分腿蹲", "weight": 40, "sets": 3, "reps": 5, "intensity": 8 },
      { "name": "泽奇深蹲", "weight": 50, "sets": 3, "reps": 6, "intensity": 7 },
      { "name": "高位下拉", "weight": 100, "sets": 3, "reps": 5, "intensity": 8 },
      { "name": "站姿单手推肩", "weight": 22, "sets": 3, "reps": 8, "intensity": 8 },
      { "name": "绳索划船", "weight": 100, "sets": 3, "reps": 5, "intensity": 8 },
      { "name": "单手过顶行走", "weight": 26, "sets": 3, "time": 40, "intensity": 7 },
      { "name": "单手农夫行走", "weight": 30, "sets": 2, "time": 40, "intensity": 7 },
      { "name": "双手农夫行走", "weight": 60, "sets": 2, "time": 20, "intensity": 7 },
      { "name": "山羊挺身负重", "weight": 20, "sets": 5, "reps": 10, "intensity": 6 },
      { "name": "俯卧腿弯举", "weight": 35, "sets": 3, "reps": 10, "intensity": 5 },
      { "name": "面拉", "weight": 20, "sets": 3, "reps": 18, "intensity": 5 },
      { "name": "单手悬挂", "sets": 2, "time": 3, "intensity": 7 }
    ]
  },
  "A": {
    "label": "A: 爆发与推链",
    "duration": 35,
    "exercises": [
      { "name": "双力臂", "intensity": 6, "sets": 5, "reps": 3 },
      { "name": "伪俄挺", "intensity": 5, "sets": 4, "reps": 5 },
      { "name": "双杠臂屈伸", "intensity": 4, "sets": 4, "reps": 8 },
      { "name": "举腿碰杆", "intensity": 4, "sets": 3, "reps": 5 },
      { "name": "单臂悬挂", "intensity": 3, "sets": 2, "time": 10 }
    ]
  },
  "B": {
    "label": "B: 控制与抗旋",
    "duration": 45,
    "exercises": [
      { "name": "进阶团身前水平", "intensity": 5, "sets": 4, "reps": 1, "time": 7 },
      { "name": "标准引体", "intensity": 4, "sets": 4, "reps": 5 },
      { "name": "单杠抗旋雨刷器", "intensity": 5, "sets": 2, "reps": 4 },
      { "name": "悬垂举腿左右弯", "intensity": 4, "sets": 2, "reps": 6 },
      { "name": "轻伪俄挺", "intensity": 3, "sets": 3, "reps": 4 }
    ]
  },
  "Jump": {
    "label": "Jump: 弹性骨刺",
    "duration": 20,
    "exercises": [
      { "name": "脚踝弹跳热身", "priority": "must", "intensity": 2, "sets": 2, "time": 30 },
      { "name": "高弹跳跳绳", "priority": "must", "intensity": 4, "sets": 5, "time": 40 },
      { "name": "双摇练习", "priority": "must", "intensity": 5, "sets": 5, "reps": 10 },
      { "name": "连续轻弹跳", "priority": "optional", "intensity": 2, "sets": 2, "time": 30 }
    ]
  }
};

    createApp({
      data() {
        return {
          plans: PLANS,
          planKeys: Object.keys(PLANS),
          currentPlan: "Gym_A",
          knowledgeDB: KNOWLEDGE_DB,
          activeKnowledgeIndex: 0,
          inputs: {},
          rating: 4,
          duration: 90,
          remark: '',
          generatedRecord: '',
          
          showConfig: false,
          configJsonStr: JSON.stringify(PLANS, null, 2),
          
          showBatchEdit: false,
          batchEditPlans: [],
          batchEditText: ''
        }
      },
      computed: {
        currentPlanConfig() {
          return this.plans[this.currentPlan];
        },
        currentExercises() {
          return this.currentPlanConfig.exercises;
        },
        currentKnowledge() {
          return this.knowledgeDB[this.activeKnowledgeIndex] || {};
        }
      },
      created() {
        // 核心修复：初始加载时，自动勾选并立刻提取配置文本，不留空白！
        this.batchEditPlans = [this.currentPlan];
        this.applyDefaults();
        this.loadSelectedPlansText();
        this.loadFromCloud(true);
      },
      methods: {
        switchPlan(key) {
          if (this.currentPlan === key) return;
          this.currentPlan = key;
          this.applyDefaults();
          this.generatedRecord = '';

          // 核心修复：点击顶部计划切换时，不仅改状态，还瞬间替换文本框内容！
          this.batchEditPlans = [key];
          this.loadSelectedPlansText();
        },
        applyDefaults() {
          this.inputs = {};
          this.currentExercises.forEach(ex => {
            this.inputs[ex.name] = {
              weight: ex.weight,
              sets:   ex.sets,
              reps:   ex.reps,
              time:   ex.time
            };
          });
          this.duration = this.currentPlanConfig.duration || 30;
          this.rating = 4;
          this.remark = '';
        },
        deload(ex) {
          const data = this.inputs[ex.name];
          if (data.reps !== undefined) {
            data.reps = Math.max(1, Math.floor(data.reps * 0.7));
          } else if (data.time !== undefined) {
            data.time = Math.max(5, Math.floor(data.time * 0.7));
          }
          if (data.weight !== undefined && data.weight > 0) {
            data.weight = Math.max(0, data.weight - 5);
          }
        },
        generateRecord() {
          const now = new Date();
          const yy  = now.getFullYear().toString().slice(-2);
          const mm  = (now.getMonth() + 1).toString().padStart(2, '0');
          const dd  = now.getDate().toString().padStart(2, '0');
          const hh  = now.getHours().toString().padStart(2, '0');
          const min = now.getMinutes().toString().padStart(2, '0');

          let totalIntensity = 0;
          const itemsText = [];

          for (const ex of this.currentExercises) {
            const val = this.inputs[ex.name];
            if (!val || val.sets <= 0 || (val.reps <= 0 && !val.time)) continue;

            let repsMultiplier = val.reps || 1;
            if (val.reps === undefined && val.time !== undefined) {
              repsMultiplier = Math.max(1, Math.round(val.time / 10));
            }
            totalIntensity += val.sets * repsMultiplier * ex.intensity;

            let itemStr = ex.name;
            if (val.weight !== undefined && val.weight > 0) {
              itemStr += ` ${val.weight}kg`;
            }

            let volumeStr = `${val.sets}x`;
            if (val.reps !== undefined) volumeStr += val.reps;
            if (val.time !== undefined) {
              volumeStr += val.reps !== undefined ? `(${val.time}s)` : `${val.time}s`;
            }

            itemsText.push(`${itemStr} ${volumeStr}`);
          }

          let tag = "适中";
          if (totalIntensity > 350)      tag = "高容量日";
          else if (totalIntensity < 200)  tag = "恢复/技术";

          let record = `[计划${this.currentPlan} | ${tag}] ${yy}.${mm}.${dd} ${hh}:${min}\n`;
          if (itemsText.length) record += itemsText.join('  ') + '\n';
          if (totalIntensity)   record += `综合强度得分: ${Math.round(totalIntensity)}\n`;

          record += `体感评分: ${'★'.repeat(this.rating)}\n`;

          if (this.rating >= 5 && totalIntensity < 250) {
            record += "⚠️ 疲劳高反馈：低容量导致高疲劳，需留意中枢神经透支状态。\n";
          }
          if (this.duration)      record += `用时: ${this.duration}分钟\n`;
          if (this.remark.trim()) record += `备注: ${this.remark}`;

          this.generatedRecord = record.trim();
        },
        async copyRecord() {
          try {
            await navigator.clipboard.writeText(this.generatedRecord);
            alert('记录已复制！');
          } catch {
            const ta = document.createElement('textarea');
            ta.value = this.generatedRecord;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            alert('记录已复制！');
          }
        },
        
        // --- 批量换行编辑逻辑 ---
        loadSelectedPlansText() {
          if (this.batchEditPlans.length === 0) {
            this.batchEditText = "";
            return;
          }
          let text = "";
          this.batchEditPlans.forEach(planKey => {
            const plan = this.plans[planKey];
            text += `// === ${planKey} ===\n`;
            plan.exercises.forEach(ex => {
              let line = ex.name;
              if (ex.weight !== undefined) line += ` 重量=${ex.weight}`;
              if (ex.sets !== undefined) line += ` 组数=${ex.sets}`;
              if (ex.reps !== undefined) line += ` 次数=${ex.reps}`;
              if (ex.time !== undefined) line += ` 时间=${ex.time}`;
              if (ex.intensity !== undefined) line += ` 强度=${ex.intensity}`;
              text += line + "\n";
            });
            text += "\n";
          });
          this.batchEditText = text.trim();
        },
      async applyBatchEdit() {
          if (this.batchEditPlans.length === 0) {
            alert("请至少勾选一个计划！");
            return;
          }
          if (!this.batchEditText.trim()) {
            alert("请输入要修改的内容！");
            return;
          }

          // 按行分割，不去掉空白行，方便精确解析
          const lines = this.batchEditText.split('\n').map(l => l.trim());
          const newExercisesMap = {};
          
          // 属性映射字典（支持中英文混合）
          const keyMap = {
            '重量': 'weight', 'weight': 'weight',
            '组数': 'sets', 'sets': 'sets',
            '次数': 'reps', 'reps': 'reps',
            '时间': 'time', 'time': 'time',
            '强度': 'intensity', 'intensity': 'intensity'
          };

          // 如果只勾选了一个计划，默认所有无标记动作都归属该计划
          let currentPlanKey = this.batchEditPlans.length === 1 ? this.batchEditPlans[0] : null;

          for (let line of lines) {
            if (!line) continue;
            
            // 匹配并切换当前计划作用域：// === Gym_A ===
            const planMatch = line.match(/^\/\/\s*===\s*(.+?)\s*===/);
            if (planMatch) {
              currentPlanKey = planMatch[1];
              if (!newExercisesMap[currentPlanKey]) newExercisesMap[currentPlanKey] = [];
              continue;
            }

            // 过滤常规注释
            if (line.startsWith('//')) continue;

            if (!currentPlanKey) continue;
            if (!newExercisesMap[currentPlanKey]) newExercisesMap[currentPlanKey] = [];

            const tokens = line.split(/\s+/);
            if (tokens.length < 1) continue;
            
            const name = tokens[0];
            const props = { name };
            
            for (let i = 1; i < tokens.length; i++) {
              const match = tokens[i].match(/^([^=:：]+)[=:：](.+)$/);
              if (match) {
                const rawKey = match[1];
                let val = match[2];
                const enKey = keyMap[rawKey];
                
                if (enKey) {
                  if (['weight', 'sets', 'reps', 'time', 'intensity'].includes(enKey)) {
                    val = isNaN(Number(val)) ? val : Number(val);
                  }
                  props[enKey] = val;
                }
              }
            }
            newExercisesMap[currentPlanKey].push(props);
          }

          let updatedPlanCount = 0;
          for (const [planKey, newExList] of Object.entries(newExercisesMap)) {
            if (this.plans[planKey]) {
              const oldExList = this.plans[planKey].exercises;
              
              // 核心重构：彻底根据文本框的新列表进行【覆写替换】，实现自由删减、排序
              const finalExercises = newExList.map(newEx => {
                // 如果旧配置有该动作，继承它的优先级和备注
                const oldEx = oldExList.find(e => e.name === newEx.name);
                if (oldEx) {
                  const merged = { ...oldEx };
                  // 强制清除旧的数值属性，严格以文本框输入为准
                  ['weight', 'sets', 'reps', 'time', 'intensity'].forEach(k => delete merged[k]);
                  return Object.assign(merged, newEx);
                } else {
                  return newEx; // 完全新增的动作
                }
              });
              
              // 覆盖替换原计划的 exercises 数组
              this.plans[planKey].exercises = finalExercises;
              updatedPlanCount++;
            }
          }

          if (updatedPlanCount === 0) {
            alert("未能识别出有效的计划修改！\n请确保多选时保留了类似 // === Gym_A === 的分隔线。");
            return;
          }

          // 同步到底层 JSON 文本框
          this.configJsonStr = JSON.stringify(this.plans, null, 2);
          
          this.applyDefaults();
          // 重新对齐格式化
          this.loadSelectedPlansText(); 
          
          // ==== 自动云同步 ====
          await this.saveToCloud(true);
          
          alert(`✅ 修改已应用并自动同步至云端！\n已按文本完全重建 ${updatedPlanCount} 个计划。`);
        },
        // --- 高级 JSON 与云端同步逻辑 ---
        // --- 高级 JSON 与云端同步逻辑 ---
        applyLocalConfig(silent = false) {
          try {
            const parsed = JSON.parse(this.configJsonStr);
            this.plans = parsed;
            this.planKeys = Object.keys(parsed);
            if (!this.planKeys.includes(this.currentPlan)) {
              this.currentPlan = this.planKeys[0];
            }
            this.applyDefaults();
            // 核心修复：云端数据覆盖后，同时刷新上方的批量修改文本框
            this.batchEditPlans = [this.currentPlan];
            this.loadSelectedPlansText();
            
            if (silent !== true) alert('本地 JSON 结构应用成功！');
          } catch (e) {
            if (silent !== true) alert('JSON格式错误，请检查大括号和逗号！\n' + e.message);
          }
        },
        async loadFromCloud(silent = false) {
          try {
            const res = await fetch(API_BASE + '/api2/kv/gym_cfg');
            if (!res.ok) throw new Error('云端无数据或接口异常');
            const data = await res.json();
            this.configJsonStr = JSON.stringify(data, null, 2);
            this.applyLocalConfig(true); // 应用时强制静默
            
            if (silent !== true) alert('云端配置加载并应用成功！');
          } catch (e) {
            if (silent !== true) alert('加载失败: ' + e.message);
            else console.log('静默加载云端数据失败（可能是首次使用或无网络），已使用默认配置');
          }
        },
        async saveToCloud(silent = false) {
          try {
            const parsed = JSON.parse(this.configJsonStr);
            const res = await fetch(API_BASE + '/api2/kv/gym_cfg', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ payload: parsed })
            });
            if (!res.ok) throw new Error('网络保存失败');
            
            if (silent !== true) alert('云端保存成功！');
          } catch (e) {
            if (silent !== true) alert('保存失败: ' + e.message);
            else console.error('静默自动保存失败:', e);
          }
        }
      }
    }).mount('#app');
  </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
    'exercise_long': r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<script>window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");</script>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>长线负荷与恢复评估模型</title>
  <script src="static/cdn/vue3.5.global.prod.js"></script>
  <style>
    body {
      background-color: #f0f4f8;
      min-height: 100vh;
      margin: 0;
      padding: 16px;
      padding-top: env(safe-area-inset-top);
      padding-bottom: env(safe-area-inset-bottom);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    .app-container {
      max-width: 600px;
      margin: 0 auto;
      box-sizing: border-box;
      padding-bottom: 40px;
    }
    h1 {
      text-align: center;
      color: #0f172a;
      font-size: 1.4rem;
      font-weight: bold;
      margin-bottom: 24px;
      letter-spacing: 0.5px;
    }

    /* 模块标题通用样式 */
    .section-title {
      font-size: 1.1rem;
      font-weight: 700;
      color: #1e40af;
      margin-bottom: 12px;
      display: flex;
      align-items: center;
    }

    /* 知识库卡片样式 */
    .knowledge-card {
      background: white;
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 24px;
      box-shadow: 0 4px 6px rgba(0,0,0,0.05);
      border-top: 4px solid #3b82f6;
    }
    .tags-container {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }
    .knowledge-tag {
      background: #f1f5f9;
      color: #475569;
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      border: 1px solid transparent;
      user-select: none;
    }
    .knowledge-tag:hover {
      background: #e2e8f0;
    }
    .knowledge-tag.active {
      background: #eff6ff;
      color: #1d4ed8;
      border-color: #bfdbfe;
    }
    .knowledge-content {
      font-size: 14.5px;
      color: #334155;
      line-height: 1.6;
      background: #f8fafc;
      padding: 14px;
      border-radius: 8px;
    }

    /* 训练计划卡片样式 */
    .plan-card {
      background: white;
      border-radius: 12px;
      margin-bottom: 16px;
      box-shadow: 0 4px 6px rgba(0,0,0,0.05);
      overflow: hidden;
    }
    .plan-header {
      background: #1e293b;
      color: white;
      padding: 12px 16px;
      font-weight: 600;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .plan-badge {
      background: rgba(255,255,255,0.2);
      padding: 2px 8px;
      border-radius: 12px;
      font-size: 12px;
    }
    .plan-body {
      padding: 16px;
    }
    .plan-detail-row {
      display: flex;
      margin-bottom: 8px;
      border-bottom: 1px solid #f1f5f9;
      padding-bottom: 8px;
      align-items: center;
    }
    .plan-detail-row:last-child {
      border-bottom: none;
      margin-bottom: 0;
      padding-bottom: 0;
    }
    .plan-label {
      flex: 0 0 100px;
      font-weight: 600;
      color: #64748b;
      font-size: 14px;
    }
    .plan-value {
      flex: 1;
      color: #0f172a;
      font-size: 14px;
      line-height: 1.5;
    }
    .plan-highlight {
      color: #ef4444;
      font-weight: 600;
    }

    /* 执行纪律警示框 */
    .alert-box {
      background: #fff1f2;
      border-left: 4px solid #f43f5e;
      padding: 12px 16px;
      border-radius: 8px;
      margin-bottom: 24px;
    }
    .alert-title {
      color: #e11d48;
      font-weight: 700;
      margin-bottom: 4px;
      font-size: 14px;
    }
    .alert-text {
      color: #881337;
      font-size: 13px;
      line-height: 1.5;
      margin: 0;
    }

    /* 表单输入样式 (针对计算器) */
    .input-group {
      display: flex;
      align-items: center;
      margin-bottom: 12px;
    }
    .input-group label {
      flex: 0 0 110px;
      font-size: 14px;
      font-weight: 600;
      color: #475569;
    }
    .input-control {
      flex: 1;
      padding: 8px 12px;
      border: 1px solid #cbd5e1;
      border-radius: 6px;
      font-size: 14px;
      outline: none;
      transition: border-color 0.2s;
    }
    .input-control:focus {
      border-color: #3b82f6;
    }
    select.input-control {
      background-color: white;
    }
    .result-box {
      margin-top: 16px;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      padding: 16px;
      border-radius: 8px;
      text-align: center;
    }
    .result-score {
      font-size: 28px;
      font-weight: 800;
      color: #1d4ed8;
      margin: 8px 0;
    }
    .result-desc {
      font-size: 14px;
      color: #1e40af;
      font-weight: 600;
    }

    /* 表格样式 */
    .data-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
    }
    .data-table th, .data-table td {
      border: 1px solid #e2e8f0;
      padding: 8px 12px;
      text-align: center;
      font-size: 13px;
    }
    .data-table th {
      background: #f8fafc;
      font-weight: 600;
      color: #475569;
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
  <div class="app-container" id="app">
    <h1>长线负荷与恢复评估模型</h1>

    <div class="alert-box">
      <div class="alert-title">核心结论</div>
      <p class="alert-text">
        你近期的状态下滑不是“状态不好”，而是前次线路的实际负荷（LS ≈ 127）对应 10-14 天的恢复周期。将间隔强行压至 7 天，必然导致中枢驱动下降。
      </p>
    </div>

    <div class="knowledge-card">
      <div class="section-title">功能面板</div>
      <div class="tags-container">
        <div 
          v-for="tab in tabs" 
          :key="tab.id"
          class="knowledge-tag" 
          :class="{ active: currentTab === tab.id }"
          @click="currentTab = tab.id"
        >
          {{ tab.name }}
        </div>
      </div>

      <div v-if="currentTab === 'theory'" class="knowledge-content">
        <strong>核心公式：</strong><br>
        总负荷 (LS) = [(时间 × 强度) + 爬升负荷 + 热负荷 + 末端刺激] × 中枢状态修正<br><br>
        <strong>1. 基础耐力：</strong>时长(h) × 强度系数(Z2:1.0, Z3:1.3, Z4:1.6) × 10<br>
        <strong>2. 爬升负荷：</strong>爬升米数 ÷ 100 × 1.2<br>
        <strong>3. 热负荷：</strong>&lt;25°C(+0) | 25-28°C(+5) | 28-30°C(+10) | 30-32°C(+15) | &gt;32°C(+20)<br>
        <strong>4. 末端刺激：</strong>冲刺/硬顶 (+5~10)<br>
        <strong>5. 中枢修正：</strong>恢复未完全时，总负荷 × 1.1~1.3
      </div>

      <div v-if="currentTab === 'mapping'" class="knowledge-content" style="padding: 0; background: none;">
        <table class="data-table">
          <thead>
            <tr>
              <th>Load Score (LS)</th>
              <th>建议恢复时间</th>
            </tr>
          </thead>
          <tbody>
            <tr><td>&lt; 60</td><td>2 - 3 天</td></tr>
            <tr><td>60 - 80</td><td>3 - 5 天</td></tr>
            <tr><td>80 - 100</td><td>5 - 7 天 (7天循环线)</td></tr>
            <tr><td>100 - 120</td><td>7 - 10 天</td></tr>
            <tr style="background: #fee2e2; font-weight: bold;"><td>120 - 140</td><td>10 - 14 天 (你当前状态)</td></tr>
            <tr><td>&gt; 140</td><td>14 天以上</td></tr>
          </tbody>
        </table>
      </div>

      <div v-if="currentTab === 'optimize'" class="knowledge-content">
        <strong>目标：将 LS 压制在 100 以内（进入 7 天恢复区）</strong><br><br>
        针对同等路线的执行调整方案：<br>
        1. <strong>不冲刺：</strong>取消末端硬顶 (-8分)<br>
        2. <strong>控制热量：</strong>选择低温时段或加强物理降温 (-10分)<br>
        3. <strong>降低强度：</strong>将整体心率区间从 Z3 降至 Z2 (-10分)<br><br>
        通过以上调整，可将原本 127 的负荷降至 100 左右，实现安全的 7 天周期循环。
      </div>
    </div>

    <div class="plan-card">
      <div class="plan-header">
        <span>动态负荷计算器 (Load Score)</span>
        <span class="plan-badge">实时预判</span>
      </div>
      <div class="plan-body">
        <div class="input-group">
          <label>运动时长 (h)</label>
          <input type="number" v-model.number="calc.time" class="input-control" step="0.5" min="0">
        </div>
        <div class="input-group">
          <label>心率强度</label>
          <select v-model.number="calc.intensity" class="input-control">
            <option value="1.0">Z2 轻松 (系数 1.0)</option>
            <option value="1.3">Z3 稳定 (系数 1.3)</option>
            <option value="1.6">Z4 阈值 (系数 1.6)</option>
          </select>
        </div>
        <div class="input-group">
          <label>爬升高度 (m)</label>
          <input type="number" v-model.number="calc.climb" class="input-control" step="100" min="0">
        </div>
        <div class="input-group">
          <label>环境温度 (°C)</label>
          <input type="number" v-model.number="calc.temp" class="input-control" step="1">
        </div>
        <div class="input-group">
          <label>末端刺激</label>
          <select v-model.number="calc.stimulus" class="input-control">
            <option value="0">无 (0)</option>
            <option value="5">中等冲刺 (+5)</option>
            <option value="8">强拉/硬顶 (+8)</option>
            <option value="10">极限硬顶 (+10)</option>
          </select>
        </div>
        <div class="input-group">
          <label>中枢恢复状态</label>
          <select v-model.number="calc.cnsFactor" class="input-control">
            <option value="1.0">完全恢复 (x1.0)</option>
            <option value="1.1">略感疲劳 (x1.1)</option>
            <option value="1.2">未完全恢复 (x1.2)</option>
            <option value="1.3">严重疲劳 (x1.3)</option>
          </select>
        </div>

        <div class="result-box">
          <div style="font-size: 13px; color: #64748b;">综合计算结果 (Load Score)</div>
          <div class="result-score">{{ finalScore }}</div>
          <div class="result-desc">对应完全恢复时间：<span style="color: #ef4444;">{{ estimatedRecovery }}</span></div>
        </div>

        <div style="margin-top: 16px; font-size: 13px; color: #64748b; text-align: center;">
          明细: 基础 {{ baseLoad.toFixed(1) }} + 爬升 {{ climbLoad.toFixed(1) }} + 热量 {{ heatLoad }} + 刺激 {{ calc.stimulus }} = {{ (baseLoad + climbLoad + heatLoad + calc.stimulus).toFixed(1) }} (乘数: x{{ calc.cnsFactor }})
        </div>
      </div>
    </div>

  </div>

  <script>
    const { createApp } = Vue;

    createApp({
      data() {
        return {
          currentTab: 'theory',
          tabs: [
            { id: 'theory', name: '模型原理' },
            { id: 'mapping', name: '恢复周期表' },
            { id: 'optimize', name: '优化策略' }
          ],
          // 默认填入你本次的实战数据
          calc: {
            time: 5,
            intensity: 1.3,
            climb: 1500,
            temp: 31,
            stimulus: 8,
            cnsFactor: 1.2
          }
        }
      },
      computed: {
        baseLoad() {
          return (this.calc.time || 0) * this.calc.intensity * 10;
        },
        climbLoad() {
          return ((this.calc.climb || 0) / 100) * 1.2;
        },
        heatLoad() {
          const t = this.calc.temp || 0;
          if (t < 25) return 0;
          if (t <= 28) return 5;
          if (t <= 30) return 10;
          if (t <= 32) return 15;
          return 20;
        },
        finalScore() {
          const rawTotal = this.baseLoad + this.climbLoad + this.heatLoad + this.calc.stimulus;
          return Math.round(rawTotal * this.calc.cnsFactor);
        },
        estimatedRecovery() {
          const ls = this.finalScore;
          if (ls < 60) return "2-3 天";
          if (ls <= 80) return "3-5 天";
          if (ls <= 100) return "5-7 天";
          if (ls <= 120) return "7-10 天";
          if (ls <= 140) return "10-14 天";
          return "14 天以上";
        }
      }
    }).mount('#app');
  </script>

<div id="authGate">
  <form class="card" id="authForm" autocomplete="off">
    <h2 id="authTitle">记录台 · 登录</h2>
    <p>使用本项目账户登录；从 App Hub 面板进入时自动免登。</p>
    <input id="authUser" placeholder="账户名" maxlength="128">
    <input id="authPass" type="password" placeholder="密码" maxlength="256">
    <div class="err" id="authErr"></div>
    <button type="submit" id="authBtn">登录</button>
  </form>
</div>
<script>
window.API_BASE = location.pathname.replace(/\/[^/]*$/, "");
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll('a[href^="/api"], a[href^="/api2"], a[href^="/tracker"], a[href^="/audiom"]').forEach(function (a) {
    a.setAttribute("href", window.API_BASE + a.getAttribute("href"));
  });
});
(function(){
  "use strict";
  var BASE = window.API_BASE;
  function showGate(setup){
    document.getElementById("authGate").classList.add("show");
    document.getElementById("authTitle").textContent = setup ? "记录台 · 首次设置" : "记录台 · 登录";
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
