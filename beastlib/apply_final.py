#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收尾结算 (用户 2026-11-08 定稿):
  1. 底座不变 (现有 1012 条主频道底座, 不扩姊妹频道); 下载战役放弃的
     4 条超长视频在 yt_base 打 excluded=1 标记 — 不参与匹配、
     不计入底座正常成员统计, 全部底座视图带「已排除」徽标可见。
  2. 仍未定的 9 条逐条以现底座 (已排除 4 条) 为唯一范围做最终核查:
     证据充分的落定 (#008, #087), 实在找不到的按用户指令删除
     (#004, #016, #027, #035, #058, #117, #122)。
  3. 连带清查: 无记录指向 4 条被排除视频; 无记录指向底座之外
     (开工前已实测, 本脚本内再断言一次)。
删除前整行导出 JSON (含 suspects 候选行) 到 EXPORT_PATH;
运行前须已做全量备份 beastlib.db.bak-final-20261108 (脚本内断言)。
盒上以 apphub 身份在项目代码目录运行 (无参数)。
结果写入 meta 'final_report'。
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as beast  # noqa: E402

EXCLUDED = ["9Ax56oRhcZc", "mGIpOtncmSM", "_FX6rml2Yjs", "KcsRxHwGz7A"]

# (seq, source_bv, video_id, 依据)
SETTLES = [
    (8, "BV146421Z7Dp", "65fN_OUawjk",
     "最终核查落定: 底座全量 Hunt 检索中唯一的主动搜捕片 (其余 3 支为 "
     "I Got Hunted 被追捕形态, FBI 篇已配 #009); 摘要细节 (100 名订阅者"
     "各 1 万美元、封闭巨型商场、10 小时搜捕、被找到者变成人体模型、"
     "Sapnap/Marques 外援、特警加热成像无人机、Jack Black 广播、"
     "给队员母亲买船) 与该片形态逐项自洽; 转录 11274 字 / 722s, "
     "密度 15.6 恰在校准带 (11.5-20.8, 中位 15.9) 中央; 该片无转写、"
     "内容覆盖不可测, 按 #002/#120 语义定案先例以标题 v2 落定"),
    (87, "BV1at421H7dW", "JTQcIR2ZxU4",
     "最终核查落定: 摘要前提 (主持人自称无家可归、买下 20.5-21.2 万美元"
     "兰博基尼、雨中首驾、快餐与昂贵衣物消费实验、同行者加勒特撞停放"
     "红卡车、停车场露营约 7 小时等车主、赔 9000 美元、卖回经销商) "
     "与 JTQcIR2ZxU4 Homeless Man Buys A Lamborghini (2019, 860s) "
     "指纹逐项对应; 底座 6 支兰博基尼标题中唯一的叙事购车片, "
     "加勒特为 2019 时期成员; BV txt 为中文译稿 (英文 shingle 不适用), "
     "7350 字 / 860s 约 8.5 字每秒, 与中文译稿密度量级自洽; "
     "深裁原评前提高度相合, 本轮以全证据定案"),
]

# (seq, source_bv, 删除依据)
DELETES = [
    (4, "BV132gQ6FEqv",
     "多主题单片: 15 个环节跨钥匙/真假钻石/硬币翻倍/数数换钱/罚球/"
     "猜杯/钱雨等六种以上机制, 与四支 Would-You-Rather 候选各自的"
     "单一主题形态互斥; 内容全量排名最高 cov 0.0009 (噪声); "
     "四候选均无转写可核; 底座内无对应全片, 删除"),
    (16, "BV16YSjBzELv",
     "唯一标题候选 ayXxwJJId_c 经摘要否决 (单个最大盲盒 vs 多盒梯度"
     "开箱、兰博基尼拖拉机终局盒); 底座标题补扫无第二候选; "
     "内容全量排名噪声级 (最高 0.0018); 真原版底座未收录, 删除"),
    (27, "BV19AWMzdEXA",
     "圆圈类 5 候选逐一排除 (zxYjTTXc-J8 密度 26.7 不可能; "
     "zajUgQLviwk 为各自一圈且已裁定 #070; Gz3yak1cl-8 为 10000 美元"
     "2018 旧片密度 28.5; gHzuabZUd6c 为 100 天双人已配 #045); "
     "内容全量排名零重合; 100 人同处一圈加体育场形态底座无对应, 删除"),
    (35, "BV1E7dFBTEur",
     "衍生回顾片: 与原片 F0OkwXKcPSE (194s) 仅部分引用关系 "
     "(实测共享 194 个 shingle, cov 0.068), BV 本体约 900 秒反应/"
     "回顾片在底座 React 补扫中无对应全片, 删除"),
    (58, "BV1Pqeg6tEpq",
     "超级碗百万美元解谜复盘: 底座 Puzzle/Super Bowl 补扫无对应"
     "复盘片; 内容全量排名最高 cov 0.005 (噪声); 疑副频道内容, 删除"),
    (117, "BV1up421X7cX",
     "多项极限实验选集: 火车/Orbeez/蜘蛛侠/液压机四段分属不同原片; "
     "内容全量排名零重合; 火车段候选 fuhE6PYnRMc 经内容排除 "
     "(共享 shingle 个位数), 删除"),
    (122, "BV1xZ4y1n7ww",
     "帽子抽店机制底座标题零命中; BV txt 为中文译稿 (英文词 32) "
     "内容比对不适用; 两支商店候选时长 (830s/1016s) 与 5049 字译稿"
     "体量不合且机制无从核验, 删除"),
]

EXPORT_PATH = "/tmp/deleted_records_final_20261108.json"
BACKUP_NAME = "beastlib.db.bak-final-20261108"


def main():
    db_path = beast.DB_PATH
    bak = os.path.join(os.path.dirname(db_path), BACKUP_NAME)
    assert os.path.exists(bak), "缺少开工全量备份: %s" % bak
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(beast.SCHEMA)
    beast._migrate(db)
    cols = {r[1] for r in db.execute("PRAGMA table_info(yt_base)")}
    if "excluded" not in cols:
        db.execute("ALTER TABLE yt_base ADD COLUMN excluded"
                   " INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # ---- 连带清查 (只读断言, 删除前)
    ph = ",".join("?" * len(EXCLUDED))
    n_point_excluded = db.execute(
        "SELECT COUNT(*) FROM records WHERE video_id IN (%s)" % ph,
        EXCLUDED).fetchone()[0]
    n_point_outside = db.execute(
        "SELECT COUNT(*) FROM records WHERE video_id IS NOT NULL"
        " AND video_id NOT IN (SELECT video_id FROM yt_base)").fetchone()[0]
    assert n_point_excluded == 0, "有记录指向被排除视频: %d" % n_point_excluded
    assert n_point_outside == 0, "有记录指向底座之外: %d" % n_point_outside

    # ---- 删除前整行导出 (先落盘, 后动库)
    export_rows = []
    for seq, bv, reason in DELETES:
        r = db.execute("SELECT * FROM records WHERE source_bv=?",
                       (bv,)).fetchone()
        assert r and r["seq"] == seq, "待删记录对不上: %s" % bv
        assert r["match_tier"] in ("suspect", "unmatched"), \
            "#%d 当前 tier=%s, 不在未定集" % (seq, r["match_tier"])
        assert r["video_id"] is None, "#%d 已有 video_id, 不应删除" % seq
        susp = [dict(x) for x in db.execute(
            "SELECT * FROM suspects WHERE source_bv=?", (bv,))]
        export_rows.append({"seq": seq, "source_bv": bv,
                            "delete_reason": reason,
                            "record": dict(r), "suspects": susp})
    with open(EXPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "note": "beastlib 收尾结算删除记录整行导出 (可恢复)",
                   "backup": BACKUP_NAME,
                   "count": len(export_rows), "records": export_rows},
                  fh, ensure_ascii=False, indent=1)
    assert os.path.getsize(EXPORT_PATH) > 1000, "导出文件异常小"

    before = dict(db.execute(
        "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
    total_before = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    settled, deleted = [], []
    with db:
        # ---- 1. 超长排除标记
        db.execute("UPDATE yt_base SET excluded=1 WHERE video_id IN (%s)"
                   % ph, EXCLUDED)
        n_ex = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE excluded=1").fetchone()[0]
        assert n_ex == 4, "排除标记数 %d != 4" % n_ex
        base_by_id = {r["video_id"]: r for r in db.execute(
            "SELECT * FROM yt_base WHERE error IS NULL AND excluded=0")}
        used = {r["video_id"]: r for r in db.execute(
            "SELECT seq, video_id FROM records WHERE video_id IS NOT NULL")}
        # ---- 2. 落定 2 条
        for seq, bv, vid, ev in SETTLES:
            row = db.execute(
                "SELECT seq, match_tier, video_id FROM records"
                " WHERE source_bv=?", (bv,)).fetchone()
            assert row and row["seq"] == seq, "记录对不上: %s" % bv
            assert row["match_tier"] in ("suspect", "unmatched"), \
                "#%d 当前 tier=%s, 不在未定集" % (seq, row["match_tier"])
            assert row["video_id"] is None
            assert vid in base_by_id, "目标不在可用底座: %s" % vid
            assert vid not in used, "目标已被 #%s 占用" % used[vid]["seq"]
            b = base_by_id[vid]
            yt_views = (str(b["view_count"])
                        if b["view_count"] is not None else None)
            yt_dur = str(b["duration"]) if b["duration"] is not None else None
            db.execute(
                "UPDATE records SET video_id=?, match_tier='matched',"
                " match_method='title_v2', yt_title=?, yt_view_count=?,"
                " yt_duration_sec=? WHERE source_bv=?",
                (vid, b["title"], yt_views, yt_dur, bv))
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            settled.append({"seq": seq, "bv": bv, "video_id": vid,
                            "yt_title": b["title"],
                            "from_tier": row["match_tier"], "evidence": ev})
        # ---- 3. 删除 7 条 (连同 suspects 候选行)
        for seq, bv, reason in DELETES:
            db.execute("DELETE FROM suspects WHERE source_bv=?", (bv,))
            cur = db.execute("DELETE FROM records WHERE source_bv=?", (bv,))
            assert cur.rowcount == 1, "删除行数异常: %s" % bv
            deleted.append({"seq": seq, "bv": bv, "reason": reason})
        db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        after = dict(db.execute(
            "SELECT match_tier, COUNT(*) FROM records GROUP BY 1").fetchall())
        total_after = db.execute(
            "SELECT COUNT(*) FROM records").fetchone()[0]
        base_active = db.execute(
            "SELECT COUNT(*) FROM yt_base WHERE excluded=0").fetchone()[0]
        report = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "用户 2026-11-08 收尾定稿: 底座不变; 4 条超长排除; "
                      "9 条未定逐条最终核查, 落定 2, 删除 7",
            "backup": BACKUP_NAME,
            "export": EXPORT_PATH,
            "before": before, "after": after,
            "total_before": total_before, "total_after": total_after,
            "excluded": {"video_ids": EXCLUDED, "count": n_ex,
                         "base_active": base_active,
                         "mechanism": "yt_base.excluded=1; app 底座页与"
                                      "报告页统计按 excluded=0 计, "
                                      "列表行带已排除徽标; ingest_base 的 "
                                      "upsert 列清单不含 excluded, 重跑不冲掉"},
            "sweeps": {"records_pointing_to_excluded": n_point_excluded,
                       "records_pointing_outside_base": n_point_outside},
            "settled": settled, "deleted": deleted,
        }
        db.execute("INSERT OR REPLACE INTO meta VALUES ('final_report', ?)",
                   (json.dumps(report, ensure_ascii=False),))
    db.close()
    print(json.dumps({"before": before, "after": after,
                      "total_before": total_before,
                      "total_after": total_after,
                      "settled": len(settled), "deleted": len(deleted),
                      "excluded": n_ex, "base_active": base_active,
                      "export": EXPORT_PATH},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
