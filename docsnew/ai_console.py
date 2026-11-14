# -*- coding: utf-8 -*-
"""AI 控制台后端：自动生成摘要 + 标题分类。

一个统一作业：逐篇处理，每篇先生成摘要、再做标题分类，
一次保存。两份批处理脚本分步走只是因为脚本化不方便；
控制台里一篇处理完再处理下一篇。筛选条件、结构判定、
Prompt、标题写法规矩、已处理缓存都与脚本一致。
差异只在运行形态：脚本走 HTTP 调文档服务，这里直接读写同一份
.md 文件（与 backend 共用 DOCS_DIR 与 FILE_INDEX），并以作业加
轮询的方式在控制台页面里跑。

凭据只从环境变量或 backend 的凭据库读（OP_API_KEY /
LOCAL_API_KEY），永不硬编码、不回显。

与脚本的一处有意差异：脚本用接口返回的时间字符串判断“最近
3 小时”，字符串是本地时间却按 UTC 解析，窗口实际被撑到约
11 小时；这里直接用文件的 update_time 时间戳，窗口就是严格
的 3 小时。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import backend


# ============================================================
# 配置（无秘密字段；凭据只走 backend 凭据库 / 环境变量）
# ============================================================

_CONFIG_DIR = os.path.dirname(backend.DOCS_DIR)
CONFIG_FILE = os.path.join(_CONFIG_DIR, "ai_config.json")
STATE_FILE = os.path.join(_CONFIG_DIR, "title_classify_processed.json")
JOBS_FILE = os.path.join(_CONFIG_DIR, "ai_jobs.json")

DEFAULT_CONFIG = {
    "provider": "flash",
    "flash_model": "gemini-3.8-flash-medium",
    "flash_base_url": "http://127.0.0.1:19877/v1",
    "flash_timeout": 290,
    "openrouter_model": "openai/gpt-6-luna",
    "openrouter_base_url": "https://openrouter.ai/api/v1",
    "openrouter_timeout": 120,
    "local_model": "gemini-3.7-flash",
    "local_base_url": "http://192.168.31.240:8083/v1",
    "local_timeout": 50,
    "concurrency": 6,
    "max_input_chars": 30000,
    "recent_3h_extra": True,
    "force_reclassify": False,
    "dry_run": False,
    "title_style": "bracket",
    "default_limit": 0,
}

_TITLE_STYLES = ("bracket", "fullwidth", "space", "bracket_plain")

# Flash 档位：本机 agy 接口提供的三个 Gemini 3.8 Flash 模型。
_FLASH_MODELS = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-low",
)

# 摘要标签的方括号（chr 写法，避免源码里出现全角括号字形）。
_LB = chr(12304)
_RB = chr(12305)


def _display_model(cfg: dict) -> str:
    if cfg.get("provider") == "flash":
        return cfg.get("flash_model", "")
    if cfg.get("provider") == "local":
        return cfg.get("local_model", "")
    return cfg.get("openrouter_model", "")


def _normalize_flash_summary(text: str) -> str:
    """Flash 专属输出归一化：只修格式，不改内容。

    实测 Flash 有三处与已存摘要格式不一致（见 2026-11-14 对比）：
    1) 主题行缺 ** —— 摘要识别依赖 **，缺了会重复生成；
    2) 主题行末尾带句号等句末标点；
    3) 关键句段内带 "1. " 序号。
    只对 Flash 路径生效，openrouter/local 的输出原样不动。
    """
    topic_tag = _LB + "主题" + _RB
    key_tag = _LB + "关键句" + _RB
    end_tag = _LB + "结论" + _RB
    lines = text.split("\n")
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_idx is not None:
        line = lines[first_idx]
        stripped = line.strip()
        if stripped.startswith(topic_tag):
            if not re.match(r"^" + re.escape(topic_tag) + r"\s*\*\*", stripped):
                line = line.replace(topic_tag, topic_tag + "** ", 1)
            lines[first_idx] = line.rstrip().rstrip("。？！；!?.")
    in_key = False
    for i, ln in enumerate(lines):
        if key_tag in ln:
            in_key = True
            continue
        if end_tag in ln:
            in_key = False
        if in_key:
            lines[i] = re.sub(r"^\s*\d+[.、]\s*", "", ln)
    return "\n".join(lines)

# 脚本里本地模型的保守并发上限（本地端点弱于云端）。
_LOCAL_CONCURRENCY_CAP = {"process": 2}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in cfg:
                if key in data:
                    cfg[key] = data[key]
    except Exception:
        pass
    return cfg


def save_config(updates: dict) -> Tuple[bool, str, dict]:
    cfg = load_config()
    for key, value in (updates or {}).items():
        if key in DEFAULT_CONFIG:
            cfg[key] = value
    if cfg.get("provider") not in ("flash", "openrouter", "local"):
        return False, "provider 只能是 flash、openrouter 或 local。", cfg
    if cfg.get("flash_model") not in _FLASH_MODELS:
        return False, "flash_model 不支持。", cfg
    if cfg.get("title_style") not in _TITLE_STYLES:
        return False, "title_style 不支持。", cfg
    for key in ("flash_timeout", "openrouter_timeout", "local_timeout"):
        try:
            cfg[key] = max(5, min(600, int(cfg[key])))
        except (TypeError, ValueError):
            return False, "%s 必须是数字。" % key, cfg
    try:
        cfg["concurrency"] = max(1, min(12, int(cfg["concurrency"])))
        cfg["max_input_chars"] = max(1000, min(100000, int(cfg["max_input_chars"])))
        cfg["default_limit"] = max(0, min(5000, int(cfg["default_limit"])))
    except (TypeError, ValueError):
        return False, "数值字段必须是数字。", cfg
    for key in ("flash_base_url", "openrouter_model", "openrouter_base_url", "local_model", "local_base_url"):
        cfg[key] = str(cfg.get(key) or "").strip()
        if not cfg[key]:
            return False, "%s 不能为空。" % key, cfg
    for key in ("recent_3h_extra", "force_reclassify", "dry_run"):
        cfg[key] = bool(cfg.get(key))
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        return False, "配置保存失败。", cfg
    return True, "", cfg


def effective_concurrency(cfg: dict, job: str) -> int:
    n = int(cfg.get("concurrency") or 6)
    if cfg.get("provider") == "local":
        n = min(n, _LOCAL_CONCURRENCY_CAP.get(job, n))
    return max(1, n)


# ============================================================
# AI Prompt（与脚本逐字一致）
# ============================================================

AI_SYSTEM_PROMPT = (
    "请将以下 AI 回答压缩成“快速浏览卡”，不要写传统长摘要。 "
    "输出： 【主题】一句话概括核心话题 "
    "【关键词】5–10 个最重要的词/短语 "
    "【关键句】2–4 句，尽量保留原回答中的核心判断、结论或关键因果 "
    "【结论】1–2 句说明最终得到什么结论 "
    "要求： "
    "* 删除背景、解释、重复内容和铺垫 "
    "* 优先保留“结论、判断、关键数据、因果关系、限制条件” "
    "* 关键句尽量接近原文，不要过度改写 "
    "* 不要泛泛总结，不要扩写 "
    "* 总长度控制在 100～200 字 "
    "* 让人 5 秒内看懂这段回答大致讲了什么。"
)

AI_PROMPT = r"""
请总结下面这篇个人笔记。

要求：

1. 第一行必须严格使用：
【主题】** 主题名称

2. 主题应该是高度概括性的中文标题，通常控制在 20～60 字。
3. 标题只保留真正的主题，不要出现“总结”“笔记”“分析如下”等前缀。
4. 第一行之后继续输出完整的 AI 总结。
5. 保留原文最重要的事实、观点、逻辑、论证和结论。
6. 不要凭空编造原文没有的信息。
7. 不要修改原文事实。
8. 不要解释你的任务。
9. 输出内容需要能够直接放到原笔记正文最开头作为 AI 总结。

原始笔记：
"""

CLASSIFY_SYSTEM_PROMPT = (
   '''【输出要求】
- 只输出两个小写英文单词：`一级 二级`，中间一个空格，例如 `pol pow`。
- 必须完整复制下面集合中的某一项，不得改写、缩写或自造。
- 领域明确但没有合适的子类时，用该领域的 misc（如 pol misc）。
- 简介为空、无意义或完全无法判断领域时，输出 unk。

【代码集合】格式为 一级 二级 + 含义。

pol 政治与治理：
pol pow 权力结构（集权、人事清洗、小组制度、权责关系、派系）
pol prop 宣传话语（官方叙事、合法性话术、政治文献、概念挪用）
pol ctrl 信息管控（审查、监控、保密、舆论引导、敏感议题）
pol gov 治理机制（属地责任、行政执行、基层治理、官僚运作、反腐）
pol law 法律与宪法（法律工具化、纸面与执行错位）
pol econ 政治经济（财政、产业政策、资本管制、基建周期、国企、税制）
pol welf 福利与人口政策（社保、医保、生育、彩礼、户籍）
pol ethn 民族与地区治理（新疆、西藏、边疆、维稳）
pol intl 外交与国际（外交、边界争端、援外、跨国制度比较）
pol hist 党史与政治史（文革、政治运动、建国后政治事件）
pol thy 理论与分析框架（政治学模型、体制演化、纠错机制）
pol misc 政治其他

his 历史：his anc 古代（含古代社会与生活）｜his qing 晚清与太平天国｜his rep 民国（人物、事件、烈士数据）｜his prc 建国后人物与事件｜his meth 历史规律与史学方法｜his misc

mil 军事：mil strat 战略与大战略｜mil war 战争、作战、军队组织｜mil tech 武器与军事技术｜mil misc

eco 经济与商业：eco fin 金融与资本市场｜eco inv 投资与房产｜eco biz 企业与商业模式｜eco macro 宏观与产业｜eco misc

soc 社会：soc class 阶层与教育｜soc media 社会心态与传播｜soc demo 人口与家庭｜soc relig 宗教｜soc labor 劳动与就业｜soc misc

psy 心理与关系：psy mind 心理机制｜psy rel 人际关系｜psy group 群体认知与偏差｜psy misc

know 方法论：know model 思维模型与元认知｜know decide 决策框架｜know pkm 个人知识库与系统设计｜know fake 认知纠错与伪命题｜know misc

exe 运动训练：exe str 力量训练｜exe endu 耐力运动｜exe plan 训练计划与动作库｜exe sci 运动科学与生物力学｜exe recov 恢复与损伤预防｜exe misc

med 身体与医疗：med oral 口腔｜med rehab 康复与疼痛｜med sleep 睡眠与生理｜med nutri 营养与食物｜med drug 药物与医疗知识｜med misc

out 户外与车：out hike 徒步登山｜out moto 摩托与摩旅｜out camp 露营｜out bike 自行车｜out auto 汽车与购车｜out misc

tec 技术与 AI：tec ai 模型与应用｜tec agent 智能体与提示词工作流｜tec cost API、中转、计费与选型｜tec dev 开发与工程、数据与文件管理｜tec net 网络与安全｜tec misc

fan 网文与小说：fan set 设定与世界观｜fan char 角色与关系｜fan plot 剧情机制与金手指｜fan pick 选书与阅读偏好｜fan gen AI 写作流水线｜fan mkt 题材与市场分析｜fan misc

cul 文学与影视：cul book 书评与文学｜cul film 影视｜cul phil 哲学｜cul misc

dai 个人记录：dai log 日记｜dai train 训练记录｜dai buy 购买与使用体验｜dai misc

adu 成人内容库：adu lib 整理与检索｜adu ana 产业与类型分析｜adu misc

unk 无法判断

【判定规则】
1. 按"核心结论在解释什么"归类，而不是它提到了什么。
2. 解释现实制度、权力、政策机制 → pol；叙述人物或事件本身 → his。
3. 为小说创作、选书、设定服务的内容（含穿越设定、AI 写小说）→ fan，不进 his、tec、eco。
4. 国家主动制定的政策 → pol；社会自发现象 → soc；市场与投资行为 → eco。财政、产业政策、资本管制属权力行为，归 pol econ。
5. 军队高层人事与权力问题 → pol pow；作战、战略、武器 → mil。
6. 训练方法与运动科学 → exe；伤病、康复、口腔、药物 → med。
7. AI 三分：写小说 → fan gen；模型选型、API、计费 → tec cost；模型能力与架构 → tec ai。
8. 摩托、汽车、徒步、露营都归 out，摩托车禁行政策也归 out moto。
9. 带日期的日记、训练流水、购买体验 → dai。
10. 政治类笔记最多，pol 下必须仔细区分，别都归 pol misc；其他领域拿不准时用该领域的 misc，别硬套子类。

【示例】
“复兴”与“持续进步”的语义辨析、政治动员逻辑 → pol prop
小组制度导致国务院被边缘化的运作机制 → pol pow
中国网络管控与全景监控体系的输出模式 → pol ctrl
中国资本管制的深层制度逻辑与国际对比 → pol econ
杨秀清：从烧炭工到天京事变的覆灭史 → his qing
白崇禧政治军事生涯解剖与 1949 年抉择 → his rep
多女主与人妻设定中的权力建构 → fan char
基于 Python 累积 messages 复用单一 Gemini 会话的实现方案 → tec dev
基于跨模型价格脱钩的 API 渠道套利与智能路由 → tec cost
从徐霞客实测到“真实体感-框架补充-现实验证”闭环 → know model
成人早期牙齿健康管理策略与阶梯治疗成本控制 → med oral
巫溪云台寺万步梯徒步体验与下山避坑指南 → out hike
日记2026 → dai log
（简介为空或仅有单个字母） → unk'''
)


# ============================================================
# 常量（与脚本一致）
# ============================================================

RECENT_3H_SECONDS = 3 * 3600
SNIPPET_CHARS = 200
STRUCTURE_HEAD_CHARS = 500
SCAN_BATCH_FACTOR = 4
REQUIRED_TAGS = ("【主题】", "【关键词】", "【结论】")

TOP_CODES = frozenset({
    "pol", "his", "mil", "eco", "soc", "psy", "know",
    "exe", "med", "out", "tec", "fan", "cul", "dai",
    "adu", "unk",
})


# ============================================================
# 文本处理工具（与脚本一致）
# ============================================================

def split_frontmatter(content: str):
    if not content.startswith("---"):
        return "", content
    m = re.match(
        r"^---\s*\n(.*?)\n---\s*\n?(.*)$",
        content,
        flags=re.DOTALL,
    )
    if not m:
        return "", content
    return m.group(1), m.group(2)


def extract_title(ai_text: str) -> Optional[str]:
    if not ai_text:
        return None
    patterns = [
        r"【主题】\s*\*\*\s*(.+?)(?:\r?\n|$)",
        r"【主题】\s*(.+?)(?:\r?\n|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, ai_text, flags=re.MULTILINE)
        if m:
            title = m.group(1).strip()
            title = re.sub(r"\*+$", "", title).strip()
            if title:
                return title
    return None


def update_frontmatter_title(content: str, new_title: str) -> str:
    yaml_title = new_title.replace("'", "''")
    replacement = f"title: '{yaml_title}'"
    m = re.match(
        r"^---\s*\n(.*?)\n---\s*\n?",
        content,
        flags=re.DOTALL,
    )
    if not m:
        raise RuntimeError("没有找到 YAML frontmatter")
    frontmatter = m.group(1)
    new_frontmatter, count = re.subn(
        r"^title:\s*.*$",
        replacement,
        frontmatter,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        new_frontmatter = replacement + "\n" + frontmatter
    body = content[m.end():]
    return "---\n" + new_frontmatter + "\n---\n" + body


def has_existing_ai_summary(content: str) -> bool:
    _, body = split_frontmatter(content)
    first_part = body.lstrip()[:500]
    return bool(
        re.search(
            r"^【主题】\s*\*\*",
            first_part,
            flags=re.MULTILINE,
        )
    )


def is_valid_quick_card(content: str) -> bool:
    _, body = split_frontmatter(content)
    head = body.lstrip()[:300]
    if not re.search(r"^【主题】\s*\*\*.+", head, flags=re.MULTILINE):
        return False
    for tag in ("【关键词】", "【关键句】", "【结论】"):
        if tag not in head:
            return False
    return True


def is_default_title(title: str) -> bool:
    t = str(title or "")
    return (not t) or t.startswith("随记_") or t.startswith("note_")


def has_summary_structure(content: str) -> bool:
    _, body = split_frontmatter(content)
    head = body.lstrip()[:STRUCTURE_HEAD_CHARS]
    for tag in REQUIRED_TAGS:
        if tag not in head:
            return False
    return True


def missing_summary_tags(content: str) -> list:
    _, body = split_frontmatter(content)
    head = body.lstrip()[:STRUCTURE_HEAD_CHARS]
    return [tag for tag in REQUIRED_TAGS if tag not in head]


def read_frontmatter_title(frontmatter: str) -> Optional[str]:
    if not frontmatter:
        return None
    m = re.search(r"^title:\s*(.*)$", frontmatter, flags=re.MULTILINE)
    if not m:
        return None
    raw = m.group(1).strip()
    if len(raw) >= 2 and raw.startswith("'") and raw.endswith("'"):
        raw = raw[1:-1].replace("''", "'")
    elif len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    return raw.strip()


# ---- 分类前缀：识别与剥离 ----

_BRACKET_HALF_RE = re.compile(r"^\[([a-z]{2,6})\s+([a-z]{2,6})\]\s*")
_BRACKET_FULL_RE = re.compile(r"^［([a-z]{2,6})\s+([a-z]{2,6})］\s*")


def strip_category_prefix(title: str):
    if not title:
        return None, title
    for pat in (_BRACKET_HALF_RE, _BRACKET_FULL_RE):
        m = pat.match(title)
        if m:
            return (m.group(1).lower(), m.group(2).lower()), title[m.end():].strip()
    parts = title.split(None, 2)
    if len(parts) == 3:
        c1, c2, rest = parts
        if (
            c1.lower() in TOP_CODES
            and re.match(r"^[a-z]{2,6}$", c2.lower())
            and rest.strip()
        ):
            return (c1.lower(), c2.lower()), rest
    return None, title


def build_title(code1: str, code2: str, base_title: str, style: str = "bracket") -> str:
    if style == "fullwidth":
        return f"［{code1} {code2}］ {base_title}"
    if style == "space":
        return f"{code1} {code2} {base_title}"
    return f"[{code1} {code2}] {base_title}"


def set_title_in_content(content: str, new_title: str) -> str:
    # 与分类脚本一致：始终用单引号包裹，YAML 安全
    #（分类前缀 "[pol welf] ..." 以 `[` 开头，不加引号会被
    # YAML 解析成数组导致 title 丢失）。
    yaml_title = new_title.replace("'", "''")
    replacement = f"title: '{yaml_title}'"
    m = re.match(
        r"^---\s*\n(.*?)\n---\s*\n?",
        content,
        flags=re.DOTALL,
    )
    if not m:
        return "---\n" + replacement + "\n---\n" + content
    frontmatter = m.group(1)
    new_frontmatter, count = re.subn(
        r"^title:\s*.*$",
        replacement,
        frontmatter,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 0:
        new_frontmatter = replacement + "\n" + frontmatter
    body = content[m.end():]
    return "---\n" + new_frontmatter + "\n---\n" + body


CODE_RE = re.compile(r"([a-zA-Z]{2,6})[\s\-_/,|]+([a-zA-Z]{2,6})")
_STRIP_CHARS = " \t：:，,。.、-—_[]［］（）()【】\"'`*"


def parse_classification(text: str) -> Tuple[str, str, str]:
    if not text or not text.strip():
        raise RuntimeError("AI 返回为空")
    first_line = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break
    if not first_line:
        raise RuntimeError("AI 返回为空")
    m = CODE_RE.search(first_line)
    if not m:
        raise RuntimeError(f"无法从 AI 返回中解析分类代号：{first_line[:200]}")
    code1 = m.group(1).lower()
    code2 = m.group(2).lower()
    label = first_line[m.end():].strip(_STRIP_CHARS)
    return code1, code2, label


# ============================================================
# 已处理 ID 持久化（与分类脚本一致，原子替换）
# ============================================================

class ProcessedStore:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.ids = self._load()

    def _load(self) -> set:
        if not self.path.exists():
            return set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("processed_ids"), list):
                raise ValueError("缓存格式错误")
            return {str(x) for x in data["processed_ids"]}
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"读取已处理缓存失败：{self.path}，{exc}；请检查文件，避免重复分类") from exc

    def contains(self, doc_id) -> bool:
        return str(doc_id) in self.ids

    def add(self, doc_id):
        key = str(doc_id)
        with self._lock:
            if key in self.ids:
                return
            new_ids = self.ids | {key}
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                tmp.write_text(
                    json.dumps({"version": 1, "processed_ids": sorted(new_ids)},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            finally:
                if tmp.exists():
                    tmp.unlink()
            self.ids = new_ids


# ============================================================
# AI 客户端（urllib：本项目只有 gunicorn + PyYAML 两个依赖）
# ============================================================

def _get_key(name: str) -> str:
    """读凭据：优先 backend 的凭据库（含环境变量优先逻辑），
    旧版 backend 没有该函数时退回只读环境变量。"""
    fn = getattr(backend, "_api_key", None)
    if callable(fn):
        try:
            return fn(name)
        except Exception:
            pass
    return os.getenv(name, "").strip()


class AIClient:
    def __init__(self, cfg: dict, x_title: str):
        self.cfg = cfg
        if cfg["provider"] == "flash":
            self.label = "3.8 Flash（本机）"
            self.base_url = cfg["flash_base_url"]
            self.api_key = ""
            self.model = cfg["flash_model"]
            self.timeout = int(cfg["flash_timeout"])
            self.extra_headers = {}
        elif cfg["provider"] == "local":
            self.label = "本地模型"
            self.base_url = cfg["local_base_url"]
            self.api_key = _get_key("LOCAL_API_KEY")
            self.model = cfg["local_model"]
            self.timeout = int(cfg["local_timeout"])
            self.extra_headers = {}
        else:
            self.label = "OpenRouter"
            self.base_url = cfg["openrouter_base_url"]
            self.api_key = _get_key("OP_API_KEY")
            self.model = cfg["openrouter_model"]
            self.timeout = int(cfg["openrouter_timeout"])
            self.extra_headers = {
                "HTTP-Referer": "http://localhost",
                "X-Title": x_title,
            }
        self.url = self.base_url.rstrip("/") + "/chat/completions"

    def chat(self, messages, temperature: float) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0",
            **self.extra_headers,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")[:1500]
            except Exception:
                body = ""
            raise RuntimeError(f"AI 请求 HTTP 错误 {e.code}：{body}")
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(
                "AI 返回格式异常：\n"
                + json.dumps(data, ensure_ascii=False)[:3000]
            )
        if not text or not text.strip():
            raise RuntimeError("AI 返回为空")
        return text.strip()

    def summarize(self, note_text: str) -> str:
        note_text = note_text[: int(self.cfg["max_input_chars"])]
        text = self.chat(
            [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": AI_PROMPT + "\n" + note_text},
            ],
            0.2,
        )
        if self.cfg.get("provider") == "flash":
            text = _normalize_flash_summary(text)
        return text

    def classify(self, title: str, snippet: str) -> Tuple[str, str, str]:
        user_prompt = (
            f"笔记标题：{title}\n"
            f"笔记开头（前 {SNIPPET_CHARS} 字）：\n"
            f"{snippet}"
        )
        raw = self.chat(
            [
                {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            0.1,
        )
        return parse_classification(raw)


# ============================================================
# 文档读写（进程内，与脚本走 HTTP 保存的效果一致）
# ============================================================

def _list_items() -> list:
    with backend.INDEX_LOCK:
        return sorted((dict(d) for d in backend.FILE_INDEX.values()),
                      key=lambda d: (d["is_pinned"], d["update_time"]), reverse=True)


def _read_doc(doc_id: str):
    """返回 (item, content)，缺失时返回 (None, None)。"""
    with backend.INDEX_LOCK:
        found = backend.current_doc(doc_id)
        if not found:
            return None, None
        item, path = found
        if not path.is_file():
            return None, None
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None, None
        return dict(item), content


def _save_doc(doc_id: str, raw_content: str) -> None:
    """与 backend.save_document 已有文档分支完全一致：解析
    frontmatter、补 id 与时间戳、YAML 重排、原子写、刷新索引。"""
    with backend.INDEX_LOCK:
        existing = backend.FILE_INDEX.get(doc_id)
        if not existing:
            raise RuntimeError("文档不在索引中")
        path = backend.doc_path(existing["path"])
        if not path.is_file():
            raise RuntimeError("物理文件丢失，请先同步索引")
        status = existing["status"]
        meta, body = backend.parse_frontmatter(raw_content)
        created = existing.get("created_time") or backend.parse_meta_timestamp(
            meta, "created_time", "created_at")
        if not created:
            created = backend.now_timestamp()
        meta["id"] = doc_id
        backend.update_meta_timestamp(meta, int(created), status)
        final_content = backend.build_frontmatter(meta, body)
        backend.write_atomic(path, final_content)
        backend.FILE_INDEX[doc_id] = backend.build_doc_index(
            path, existing["path"], final_content, meta)


def _is_recent_3h(item: dict) -> bool:
    ts = item.get("update_time") or item.get("created_time")
    if not ts:
        return False
    try:
        age = time.time() - float(ts)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= RECENT_3H_SECONDS


# ============================================================
# 统一处理：逐篇先摘要、后分类（与两份脚本一致的决策）
# ============================================================

def _summary_decide(item: dict, content: str, recent_extra: bool):
    """返回 (action, reason)：action 为 process/skip。"""
    valid_card = is_valid_quick_card(content)
    existing_summary = has_existing_ai_summary(content)
    is_default = is_default_title(item.get("title"))
    recent = recent_extra and _is_recent_3h(item)
    if valid_card:
        return "skip", "前300字已是完整快速浏览卡"
    if existing_summary:
        if recent:
            return "process", "最近3小时内，顶部总结结构不合格，重新生成"
        return "skip", "顶部已有总结但结构不合格，且不在3小时内"
    if is_default:
        return "process", "默认标题，顶部无总结"
    if recent:
        return "process", "非默认标题，最近3小时内且无总结"
    return "skip", "非默认标题且不在3小时内"


def _summary_phase(content, item, ai, recent_extra, log):
    """摘要阶段：纯变换，不保存。返回 (new_content, outcome, detail)，
    outcome 为 success（已生成）或 skip。"""
    action, reason = _summary_decide(item, content, recent_extra)
    if action != "process":
        return content, "skip", reason
    log("  [摘要] 调用 AI...")
    ai_text = ai.summarize(content)
    log("  [摘要] AI 返回：%s 字符" % f"{len(ai_text):,}")
    new_title = extract_title(ai_text)
    if not new_title:
        raise RuntimeError("AI 返回中没有找到主题标题")
    log("  [摘要] 新标题：" + new_title)
    updated = update_frontmatter_title(content, new_title)
    frontmatter, body = split_frontmatter(updated)
    if frontmatter:
        updated = "---\n" + frontmatter + "\n---\n" + ai_text + "\n\n" + body.lstrip()
    else:
        updated = ai_text + "\n\n" + body.lstrip()
    return updated, "success", new_title


def _classify_decide(content, item, store, force):
    """分类阶段的纯判定（不调 AI）。返回 (ready/skip, reason)。"""
    doc_id = item.get("id")
    if doc_id is not None and store.contains(doc_id) and not force:
        return "skip", "已在分类缓存中"
    if not has_summary_structure(content):
        missing = missing_summary_tags(content)
        return "skip", "开头缺少 " + " ".join(missing) + "，尚未总结"
    frontmatter, body = split_frontmatter(content)
    title_in_fm = read_frontmatter_title(frontmatter)
    current_title = (title_in_fm or item.get("title") or "").strip()
    if not current_title:
        return "skip", "标题为空"
    prefix, _ = strip_category_prefix(current_title)
    if prefix and not force:
        return "skip", "已有分类前缀"
    snippet = body.lstrip()[:SNIPPET_CHARS]
    if not snippet.strip():
        return "skip", "正文前 %d 字为空" % SNIPPET_CHARS
    return "ready", ""


def _classify_phase(content, item, ai, force, title_style, log):
    """分类阶段：纯变换，不保存、不写缓存。返回 (new_content, outcome, detail)。"""
    frontmatter, body = split_frontmatter(content)
    title_in_fm = read_frontmatter_title(frontmatter)
    current_title = (title_in_fm or item.get("title") or "").strip()
    if not current_title:
        return content, "skip", "标题为空，无法分类"
    prefix, base_title = strip_category_prefix(current_title)
    if prefix and not force:
        return content, "skip", "已有分类前缀：" + current_title
    snippet = body.lstrip()[:SNIPPET_CHARS]
    log("  [分类] 基础标题：" + base_title)
    log("  [分类] 调用 AI（前 %d 字）..." % len(snippet))
    code1, code2, label = ai.classify(base_title, snippet)
    new_title = build_title(code1, code2, base_title, title_style)
    log("  [分类] AI 分类：%s %s（%s）" % (code1, code2, label or "无标签"))
    log("  [分类] 新标题：" + new_title)
    return set_title_in_content(content, new_title), "success", new_title


def _process_scan(items, store, force, recent_extra, concurrency):
    """统一扫描：逐篇判定摘要与分类两个阶段，返回 (targets, counts)。
    摘要会重写标题时，分类按“摘要后”判定为可跑——新标题没有前缀
    且未经分类，缓存针对旧标题的状态不再适用。"""
    counts = {"process": 0, "skip": 0, "error": 0,
              "summary_process": 0, "classify_ready": 0,
              "cache_skipped": 0, "prefix_skipped": 0}

    def _check(item):
        _, content = _read_doc(item["id"])
        if not content:
            return {"id": item["id"], "title": str(item.get("title") or ""),
                    "summary": ("error", "正文为空或无法读取"),
                    "classify": ("skip", ""), "target": True, "error": True}
        s_action, s_reason = _summary_decide(item, content, recent_extra)
        if s_action == "process":
            c_action, c_reason = "ready", "摘要后分类"
        else:
            c_action, c_reason = _classify_decide(content, item, store, force)
        return {"id": item["id"], "title": str(item.get("title") or ""),
                "summary": (s_action, s_reason), "classify": (c_action, c_reason),
                "target": s_action == "process" or c_action == "ready",
                "error": False}

    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futs = {executor.submit(_check, it): i for i, it in enumerate(items)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                results[i] = {"id": items[i]["id"],
                              "title": str(items[i].get("title") or ""),
                              "summary": ("error", "%s: %s" % (type(e).__name__, e)),
                              "classify": ("skip", ""), "target": True, "error": True}
    targets = []
    for r in results:
        if r["error"]:
            counts["error"] += 1
            counts["process"] += 1
        elif r["target"]:
            counts["process"] += 1
        else:
            counts["skip"] += 1
        if r["summary"][0] == "process":
            counts["summary_process"] += 1
        if r["classify"][0] == "ready":
            counts["classify_ready"] += 1
        if r["classify"][1] == "已在分类缓存中":
            counts["cache_skipped"] += 1
        if r["classify"][1] == "已有分类前缀":
            counts["prefix_skipped"] += 1
        if r["target"]:
            targets.append({"id": r["id"], "title": r["title"],
                            "summary": r["summary"][0],
                            "summary_reason": r["summary"][1],
                            "classify": r["classify"][0],
                            "classify_reason": r["classify"][1]})
    return targets, counts


def _process_one(item, ai, opts, store, log):
    """处理一篇：先摘要、后分类，最多保存一次。返回 result dict。
    任一阶段抛错则整篇记失败、什么都不写，下次运行可重试。"""
    doc_id = item["id"]
    _, content = _read_doc(doc_id)
    if not content:
        return {"outcome": "skip", "summary": ("skip", "正文为空"),
                "classify": ("skip", ""), "saved": False}
    new_content, s_out, s_detail = _summary_phase(
        content, item, ai, opts["recent_3h_extra"], log)
    if s_out == "success":
        c_action = "ready"
    else:
        c_action, _c_reason = _classify_decide(
            new_content, item, store, opts["force"])
    if c_action == "ready":
        new_content, c_out, c_detail = _classify_phase(
            new_content, item, ai, opts["force"], opts["title_style"], log)
    else:
        c_out, c_detail = "skip", _c_reason
    changed = (s_out == "success") or (c_out == "success")
    saved = False
    if changed and not opts["dry_run"]:
        _save_doc(doc_id, new_content)
        saved = True
    if c_out == "success" and not opts["dry_run"]:
        try:
            store.add(doc_id)
        except Exception as e:
            log("[缓存写入失败] ID=%s：%s；下次可能重复检查" % (doc_id, e))
    return {"outcome": "success" if changed else "skip",
            "summary": (s_out, s_detail), "classify": (c_out, c_detail),
            "saved": saved}


# ============================================================
# 作业引擎（一次只跑一个作业）
# ============================================================

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_RUNNING = {"job_id": None}


def _job_log(job: dict, message: str) -> None:
    with job["lock"]:
        job["log"].append(message)


def _persist_job_summary(job: dict) -> None:
    try:
        summaries = []
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                summaries = data
        summaries.insert(0, {
            "id": job["id"],
            "job": job["job"],
            "status": job["status"],
            "total": job["total"],
            "counters": dict(job["counters"]),
            "provider": job.get("provider"),
            "model": job.get("model"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "elapsed_ms": job.get("elapsed_ms"),
            "options": job.get("options"),
        })
        tmp = JOBS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(summaries[:10], fh, ensure_ascii=False, indent=2)
        os.replace(tmp, JOBS_FILE)
    except Exception:
        pass


def _load_job_summaries() -> list:
    try:
        with open(JOBS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _run_job_thread(job: dict) -> None:
    cfg = load_config()
    job["provider"] = cfg["provider"]
    opts = job["options"]
    try:
        ai = AIClient(cfg, "Note AI Console")
        job["model"] = ai.model
        if not ai.api_key and cfg["provider"] != "flash":
            _job_log(job, "[致命] 当前 Provider 的 api_key 为空，请先在凭据区保存或设置环境变量。")
            job["status"] = "failed"
            return
        _job_log(job, "=" * 60)
        _job_log(job, f"AI Provider：{ai.label} ({cfg['provider']})  模型：{ai.model}")
        _job_log(job, "=" * 60)
        job["started_at"] = int(time.time())
        started = time.time()
        _run_process(job, cfg, ai, opts)
        job["elapsed_ms"] = int((time.time() - started) * 1000)
        job["status"] = "done"
        _job_log(job, "=" * 60)
        _job_log(job, f"处理完成：成功 {job['counters']['success']}，"
                      f"跳过 {job['counters']['skip']}，"
                      f"失败 {job['counters']['error']}，"
                      f"耗时 {job['elapsed_ms'] / 1000:.1f} 秒")
    except Exception as e:
        _job_log(job, f"[作业异常] {type(e).__name__}: {e}")
        job["status"] = "failed"
    finally:
        job["finished_at"] = int(time.time())
        with _JOBS_LOCK:
            if _RUNNING.get("job_id") == job["id"]:
                _RUNNING["job_id"] = None
        _persist_job_summary(job)


def _run_process(job: dict, cfg: dict, ai: AIClient, opts: dict) -> None:
    recent_extra = bool(opts.get("recent_3h_extra", cfg["recent_3h_extra"]))
    force = bool(opts.get("force_reclassify", cfg["force_reclassify"]))
    dry_run = bool(opts.get("dry_run", cfg["dry_run"]))
    title_style = str(opts.get("title_style", cfg["title_style"]))
    limit = int(opts.get("limit") or 0)
    concurrency = effective_concurrency(cfg, "process")
    store = ProcessedStore(STATE_FILE)
    items = _list_items()
    _job_log(job, "共获取 %d 篇文档" % len(items))
    targets, counts = _process_scan(items, store, force, recent_extra, concurrency)
    _job_log(job, "待处理：%d 篇（摘要 %d 篇，分类 %d 篇）；跳过 %d 篇" % (
        counts["process"], counts["summary_process"],
        counts["classify_ready"], counts["skip"]))
    if limit and limit > 0:
        targets = targets[:limit]
    job["total"] = len(targets)
    _job_log(job, "目标文档：%d 篇（并发 %d，逐篇先摘要后分类）" % (len(targets), concurrency))
    if not targets:
        _job_log(job, "没有需要处理的笔记。")
        return
    by_id = {it["id"]: it for it in items}
    run_opts = {"recent_3h_extra": recent_extra, "force": force,
                "dry_run": dry_run, "title_style": title_style}

    def _one(index, target):
        item = by_id[target["id"]]

        def log(msg):
            _job_log(job, msg)
        _job_log(job, "")
        _job_log(job, "=" * 60)
        _job_log(job, "[%d/%d] %s" % (index, len(targets), item.get("title") or ""))
        _job_log(job, "ID: %s" % item.get("id"))
        try:
            res = _process_one(item, ai, run_opts, store, log)
            result = res["outcome"]
            detail = "摘要：" + res["summary"][1][:80] + "；分类：" + res["classify"][1][:80]
        except Exception as e:
            result, detail = "error", "%s: %s" % (type(e).__name__, e)
            res = {"summary": ("error", detail), "classify": ("skip", ""),
                   "saved": False}
            _job_log(job, "  [错误] " + detail)
        with job["lock"]:
            job["counters"][result] += 1
            job["done"] += 1
            if res["summary"][0] == "success":
                job["counters"]["summary_success"] = job["counters"].get("summary_success", 0) + 1
            if res["classify"][0] == "success":
                job["counters"]["classify_success"] = job["counters"].get("classify_success", 0) + 1
            job["results"].append({
                "id": item.get("id"), "title": str(item.get("title") or ""),
                "outcome": result,
                "summary": res["summary"][0],
                "summary_detail": str(res["summary"][1])[:200],
                "classify": res["classify"][0],
                "classify_detail": str(res["classify"][1])[:200],
                "saved": res["saved"]})
        _job_log(job, "  [%s] %s" % (result, detail[:120]))
        return result

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futs = [executor.submit(_one, i, t) for i, t in enumerate(targets, 1)]
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as e:
                _job_log(job, "[线程异常] %s: %s" % (type(e).__name__, e))
    _job_log(job, "阶段统计：摘要生成 %d 篇，分类完成 %d 篇" % (
        job["counters"].get("summary_success", 0),
        job["counters"].get("classify_success", 0)))


# ============================================================
# HTTP 处理（供 backend dispatcher 调用）
# ============================================================

def _cred_status_all() -> dict:
    try:
        out = dict(backend._cred_status())
    except AttributeError:
        out = {}
    for name in ("OP_API_KEY", "LOCAL_API_KEY"):
        if name not in out:
            env_set = bool(os.getenv(name, "").strip())
            out[name] = {"configured": env_set, "source": "env" if env_set else None}
    return out


def _cache_info() -> dict:
    try:
        store = ProcessedStore(STATE_FILE)
        return {"exists": os.path.exists(STATE_FILE), "count": len(store.ids)}
    except RuntimeError:
        return {"exists": os.path.exists(STATE_FILE), "count": 0, "error": True}


def handle(action: str, method: str, args: dict, data):
    if action == "config" and method == "GET":
        return 200, {"ok": True, "config": load_config(),
                     "credentials": _cred_status_all(),
                     "cache": _cache_info()}
    if action == "config" and method == "POST":
        ok, err, cfg = save_config(data if isinstance(data, dict) else {})
        if not ok:
            return 400, {"ok": False, "error": err}
        return 200, {"ok": True, "config": cfg}
    if action == "scan" and method == "POST":
        return _handle_scan(data if isinstance(data, dict) else {})
    if action == "run" and method == "POST":
        return _handle_run(data if isinstance(data, dict) else {})
    if action == "status" and method == "GET":
        return _handle_status(args or {})
    if action == "jobs" and method == "GET":
        with _JOBS_LOCK:
            running = _RUNNING.get("job_id")
        return 200, {"ok": True, "jobs": _load_job_summaries(), "running": running}
    return 400, {"ok": False, "error": "不支持的请求"}


def _handle_scan(data: dict):
    job_type = data.get("job", "process")
    if job_type != "process":
        return 400, {"ok": False, "error": "job 只能是 process"}
    cfg = load_config()
    started = time.time()
    items = _list_items()
    force = bool(data.get("force_reclassify", cfg["force_reclassify"]))
    recent_extra = bool(data.get("recent_3h_extra", cfg["recent_3h_extra"]))
    concurrency = effective_concurrency(cfg, "process")
    try:
        store = ProcessedStore(STATE_FILE)
    except RuntimeError as e:
        return 500, {"ok": False, "error": str(e)}
    targets, counts = _process_scan(items, store, force, recent_extra, concurrency)
    return 200, {"ok": True, "job": "process", "total_docs": len(items),
                 "counts": counts, "targets": targets[:200],
                 "targets_total": len(targets),
                 "scan_ms": int((time.time() - started) * 1000),
                 "provider": cfg["provider"],
                 "model": _display_model(cfg)}

def _handle_run(data: dict):
    job_type = data.get("job", "process")
    if job_type != "process":
        return 400, {"ok": False, "error": "job 只能是 process"}
    with _JOBS_LOCK:
        if _RUNNING.get("job_id"):
            return 409, {"ok": False, "error": "已有作业在运行，请等它完成。",
                         "job_id": _RUNNING["job_id"]}
        job_id = "%s-%d" % (job_type, int(time.time() * 1000))
        job = {
            "id": job_id, "job": job_type, "status": "running",
            "options": data, "created_at": int(time.time()),
            "started_at": None, "finished_at": None, "elapsed_ms": None,
            "total": 0, "done": 0,
            "counters": {"success": 0, "skip": 0, "error": 0},
            "log": [], "results": [], "lock": threading.Lock(),
            "provider": None, "model": None,
        }
        _JOBS[job_id] = job
        if len(_JOBS) > 20:
            ordered = sorted(_JOBS.values(), key=lambda j: j["created_at"])
            for old in ordered[: len(ordered) - 20]:
                if old["id"] != job_id and old["status"] in ("done", "failed"):
                    del _JOBS[old["id"]]
        _RUNNING["job_id"] = job_id
    thread = threading.Thread(target=_run_job_thread, args=(job,), daemon=True)
    thread.start()
    return 200, {"ok": True, "job_id": job_id}


def _handle_status(args: dict):
    job_id = args.get("job_id")
    with _JOBS_LOCK:
        job = _JOBS.get(job_id) if job_id else None
    if not job:
        return 404, {"ok": False, "error": "作业不存在"}
    try:
        after = max(0, int(args.get("after") or 0))
    except (TypeError, ValueError):
        after = 0
    with job["lock"]:
        log_lines = list(job["log"][after:])
        log_total = len(job["log"])
        snapshot = {
            "id": job["id"], "job": job["job"], "status": job["status"],
            "total": job["total"], "done": job["done"],
            "counters": dict(job["counters"]),
            "started_at": job["started_at"], "finished_at": job["finished_at"],
            "elapsed_ms": job["elapsed_ms"],
            "provider": job.get("provider"), "model": job.get("model"),
            "log": log_lines, "log_total": log_total,
        }
        if job["status"] in ("done", "failed") or args.get("results") == "1":
            snapshot["results"] = list(job["results"])
    return 200, {"ok": True, "job": snapshot}
