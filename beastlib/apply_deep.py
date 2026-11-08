#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第二段深裁应用 (用户 2026-11-08 点名授权 "不缺的几条你直接分析匹配"):
对标题 v2 剩余 18 条不确定记录, 用最强可用证据逐条裁定后落库。
证据构成: content_match 683 转写全量排名 + 定向 pairwise 覆盖 +
250 词分块归属 + BV md 摘要逐项语义 + 底座标题补扫 + 时长密度。
结论: 匹配 7 (内容 4 / 语义 3) · 归类 2 (合集 1 / 底座外 1) · 保留 9。
全文与逐条理由写入 meta 'deep_report'。运行前须已备份 DB 且
apply_titlev2.py 已应用。盒上以 apphub 身份在项目代码目录运行。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

# (seq, source_bv, video_id, cov, shared) -- shingle 覆盖决定性 + 分块一致
MATCHES_CONTENT = [
    (44, "BV1KJdeYWE14", "figrl0aOULs", 0.7351, 1587),
    (77, "BV1YBnSzAExb", "zo7i8VTpfNM", 0.6665, 3212),
    (86, "BV1asjNzbESL", "DZIASl9q90s", 0.6838, 3162),
    (103, "BV1jx3dzAEoK", "FWAdfuPpLOc", 0.6984, 2716),
]

# (seq, source_bv, video_id, 证据) -- 无转写可覆盖, 摘要语义决定性
MATCHES_SEMANTIC = [
    (2, "BV11b421H7YB", "PWirijQkH4M",
     "摘要六道关卡逐项吻合 (猴子杠/44格记忆地砖/红球/滚木运钱/死亡之墙/伞形饼干), "
     "含原片引文 'most deadly obstacle course in the world'; KOEfDvr4DcQ 为恐惧主题, "
     "与摘要无一对应; 密度 12.2 在校准带内"),
    (32, "BV1DM4m1k7hb", "Hwybp38GnZw",
     "摘要 15 事件逐项决定性 (金票/千秒捉迷藏/糖墙/巨型曼妥思投可乐瓶/蛋糕马桶/"
     "拉姆齐甜点决赛/50万美元回购工厂), 底座内旺卡题材唯一长片; 密度 26.4 超校准带, "
     "已注明异常 (疑 BV 版含加长段落); 该片不在 702 下载内, 无转写可做覆盖"),
    (120, "BV1wFzxY7EN7", "0BjlBnfHcHM",
     "决赛段逐项吻合: 球迷射靶先中三靶击败 C 罗赢 100 万美元; 同批已证 DZIASl9q90s/"
     "FWAdfuPpLOc 均为 '标题只点名决赛段' 的明星合集片, 本条同型; 密度 14.1 在带内; "
     "原共用候选 _c2e1NcI3Sc 经内容比对零共享 shingle 已排除; 该片无转写, 覆盖不可测"),
]

# (seq, source_bv, method, 证据)
CLASSIFY = [
    (17, "BV16o1PBfEhv", "compilation",
     "合集: 摘要自述 '挑战合集'; 环节跨托盘/叠 iPhone/卧推/戒指/Orbeez/相扑多格式; "
     "转录 21,190 字对任一单集密度 >=23.3 超上限, 唯 Carry 两集连播 (1747s, 密度 12.1) 自洽"),
    (42, "BV1KGKV6PEe3", "off_base",
     "底座外: Operation Smile 墨西哥唇腭裂公益片, 主持人 Darren/Dan 非主频道形态; "
     "底座手术/医疗类标题零命中; 与 683 转写零共享 shingle; 疑 Beast Philanthropy 系内容"),
]

# (seq, source_bv, 保留原因) -- 只记报告, 不动 DB
UNRESOLVED = [
    (4, "BV132gQ6FEqv",
     "摘要呈单片叙事 (Chandler 欠款线 E07->E12->E14 与数数线 E05->E15 贯穿首尾), "
     "疑 Would-You-Rather 系列某一单集; 但四支候选均无转写, 长度对三支兼容, "
     "无法指认; 亦不能排除官方精选合集"),
    (8, "BV146421Z7Dp",
     "封闭商场猎杀 100 名订阅者: 底座 'mall/hunt' 标题零命中; 唯一近似候选 "
     "65fN_OUawjk《I Hunted 100 People!》(722s, 密度 15.6 兼容) 无转写可核, 不硬配"),
    (16, "BV16YSjBzELv",
     "梯度开箱 ($100->$250,000, 兰博基尼拖拉机终局盒): 唯一标题候选 ayXxwJJId_c "
     "经摘要否决 (单个最大盲盒 vs 多盒梯度); 真原版底座标题无收录"),
    (27, "BV19AWMzdEXA",
     "100 人同处巨型圆圈 + 体育场抓人反转: zxYjTTXc-J8 密度 26.7 不可能; "
     "zajUgQLviwk 为各自一圈且已裁定 #070; 两候选均无转写, 内容全量排名零重合"),
    (35, "BV1E7dFBTEur",
     "衍生回顾视频: 原片 F0OkwXKcPSE (194s) 仅被部分引用 (其 shingle 35.7% 出现在 "
     "BV 文本中), BV 本体为约 15 分钟反应/回顾片, 底座内无对应全片"),
    (58, "BV1Pqeg6tEpq",
     "超级碗百万美元解谜复盘: 底座 'puzzle/super bowl' 无对应复盘片; 内容零重合; "
     "可能在副频道, 不足以断言独家"),
    (87, "BV1at421H7dW",
     "早期兰博基尼片: 前提与 JTQcIR2ZxU4《Homeless Man Buys A Lamborghini》(2019) "
     "高度相合, 但撞车/停车场露营段落无从核验 (该片无转写; BV txt 为中文译稿, "
     "英文 shingle 仅 21 词, 内容比对与密度尺均不适用)"),
    (117, "BV1up421X7cX",
     "多项极限实验选集形态: 与全部 683 转写零重合; 火车段经内容排除 fuhE6PYnRMc "
     "(Train Vs Giant Pit, 共享 shingle 1); 底座无对应标题"),
    (122, "BV1xZ4y1n7ww",
     "帽子抽店购物挑战: 底座标题零命中该机制; BV txt 为中文译稿 (英文词 32), "
     "内容比对不适用; 早期短体量, 疑未被枚举收录"),
]


def main():
    db = sqlite3.connect(beast.DB_PATH)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    base_by_id = {r["video_id"]: r for r in db.execute(
        "SELECT * FROM yt_base WHERE error IS NULL")}
    used = {r["video_id"]: r["seq"] for r in db.execute(
        "SELECT seq, video_id FROM records WHERE video_id IS NOT NULL")}
    before = dict(db.execute(
        "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
    done = []
    with db:
        for seq, bv, vid, cov, shared in MATCHES_CONTENT:
            assert vid in base_by_id and vid not in used, vid
            row = db.execute("SELECT seq, match_tier FROM records"
                             " WHERE source_bv=?", (bv,)).fetchone()
            assert row and row["seq"] == seq and \
                row["match_tier"] in ("suspect", "unmatched"), (seq, bv)
            b = base_by_id[vid]
            db.execute(
                "UPDATE records SET video_id=?, match_tier='matched',"
                " match_method='content', content_cov=?, content_shared=?,"
                " yt_title=?, yt_view_count=?, yt_duration_sec=?"
                " WHERE source_bv=?",
                (vid, cov, shared, b["title"],
                 str(b["view_count"]) if b["view_count"] is not None else None,
                 str(b["duration"]) if b["duration"] is not None else None,
                 bv))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            done.append({"seq": seq, "bv": bv, "video_id": vid,
                         "method": "content", "cov": cov, "shared": shared})
        for seq, bv, vid, why in MATCHES_SEMANTIC:
            assert vid in base_by_id and vid not in used, vid
            row = db.execute("SELECT seq, match_tier FROM records"
                             " WHERE source_bv=?", (bv,)).fetchone()
            assert row and row["seq"] == seq and \
                row["match_tier"] in ("suspect", "unmatched"), (seq, bv)
            b = base_by_id[vid]
            db.execute(
                "UPDATE records SET video_id=?, match_tier='matched',"
                " match_method='title_v2', yt_title=?, yt_view_count=?,"
                " yt_duration_sec=? WHERE source_bv=?",
                (vid, b["title"],
                 str(b["view_count"]) if b["view_count"] is not None else None,
                 str(b["duration"]) if b["duration"] is not None else None,
                 bv))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            done.append({"seq": seq, "bv": bv, "video_id": vid,
                         "method": "title_v2", "evidence": why})
        for seq, bv, method, why in CLASSIFY:
            row = db.execute("SELECT seq, match_tier FROM records"
                             " WHERE source_bv=?", (bv,)).fetchone()
            assert row and row["seq"] == seq and \
                row["match_tier"] in ("suspect", "unmatched"), (seq, bv)
            db.execute(
                "UPDATE records SET video_id=NULL, match_tier='no_original',"
                " match_method=?, yt_title=NULL, yt_view_count=NULL,"
                " yt_duration_sec=NULL, content_cov=NULL, content_shared=NULL"
                " WHERE source_bv=?", (method, bv))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            done.append({"seq": seq, "bv": bv, "classify": method,
                         "evidence": why})
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        after = dict(db.execute(
            "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
        report = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "第二段深裁 (用户点名授权): content_match 683 全量排名 + "
                      "定向覆盖/分块归属 + md 摘要语义 + 底座补扫",
            "before": before, "after": after, "decisions": done,
            "unresolved": [{"seq": s, "bv": b, "reason": r}
                           for s, b, r in UNRESOLVED],
            "notes": [
                "#086/#120 之争结论: 二者并非同一原版的两个搬运本; #086 内容决定性 "
                "匹配 DZIASl9q90s (内马尔决赛段命名), #120 与 _c2e1NcI3Sc 零共享 "
                "shingle 排除后, 按决赛段+同型命名+密度匹配 0BjlBnfHcHM",
                "#027: zajUgQLviwk 已按第一段裁定归 #070; #027 内容无任何候选, 保留",
                "#032: 按授权以覆盖/语义定案——候选无转写, 以摘要决定性语义落定并注明密度异常",
            ]}
        db.execute("INSERT OR REPLACE INTO meta VALUES ('deep_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({"before": before, "after": after,
                      "decisions": len(done),
                      "unresolved": len(UNRESOLVED)},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
