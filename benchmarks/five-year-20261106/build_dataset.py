"""五年合成语料和事先冻结的答案；不读取真实聊天。"""
import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent
START = dt.date(2021, 11, 6)
END = dt.date(2026, 11, 6)
DAYS = (END - START).days
records, cases = [], []
foods = ["番茄鸡蛋面", "牛肉饭", "南瓜粥", "豆腐汤", "香菇青菜", "玉米沙拉"]
places = ["滨江公园", "市图书馆", "北山步道", "青石书店", "河畔咖啡馆", "社区球场"]
books = ["山海散记", "星河来信", "秋日笔记", "小城故事", "远方的风", "海边日记"]
for day in range(DAYS):
    date = START + dt.timedelta(days=day)
    stamp = f"{date.year}年{date.month}月{date.day}日"
    f, p, b = foods[day % 6], places[(day // 3) % 6], books[(day // 7) % 6]
    texts = [
        f"{stamp}，我午饭吃了{f}，觉得味道不错。",
        f"{stamp}，我下班后去了{p}，在那里待了半小时。",
        f"{stamp}，我睡前读了《{b}》的几页，记了阅读心得。",
        f"{stamp}，我走了{3500 + day % 13 * 400}步，计划继续保持散步习惯。",
        f"{stamp}，我给阳台上的{['薄荷','绿萝','吊兰'][day % 3]}浇水，观察了叶子的变化。",
        f"{stamp}，我用{['水彩','铅笔','钢笔'][day % 3]}画了一幅{['风景','静物','小猫'][day % 3]}。",
        f"{stamp}，我把桌面整理好了，并备份了今天的工作文件。",
        f"{stamp}，我听了{['钢琴曲','爵士乐','轻音乐'][day % 3]}，感觉比下午轻松。",
        f"{stamp}，我准备了明天的早餐，买了{['牛奶','酸奶','豆浆'][day % 3]}和面包。",
        f"{stamp}，我和朋友聊了{['旅行','电影','运动'][day % 3]}，约好以后再见。",
    ]
    for slot, text in enumerate(texts):
        records.append({"key": f"d{day:04d}s{slot}", "day": day, "text": text,
                        "importance": 0.5, "owner": "A", "topic": "日常"})

def override(day, slot, text, importance=0.5, topic="事实"):
    key = f"d{day:04d}s{slot}"
    row = records[day * 10 + slot]
    date = START + dt.timedelta(days=day)
    row.update(text=f"{date.year}年{date.month}月{date.day}日，我说：{text}",
               importance=importance, topic=topic)
    return key

def case(category, question, answers, keys, aliases=None):
    cases.append({"id": f"Q{len(cases)+1:03d}", "category": category,
                  "question": question, "answers": answers, "keys": keys,
                  "aliases": aliases or [], "positive": bool(keys)})

traits = [
    ("母亲的生日", "六月十七日", "我妈妈在哪一天过生日？", ["6月17日", "六月17日"]),
    ("第一次养的猫的名字", "团子", "我家第一只猫叫什么？", []),
    ("大学所学的专业", "材料科学", "我在大学学的是哪门专业？", []),
    ("家乡", "宜春", "我老家是哪座城市？", []),
    ("最喜欢的作家", "汪曾祺", "我最欣赏哪位写散文的作家？", []),
    ("小时候的小名", "阿禾", "我童年时家里人怎么叫我？", []),
    ("第一份工作的岗位", "测试工程师", "我刚工作时做的是什么岗位？", []),
    ("第一次学会的乐器", "口琴", "我最早学会演奏的乐器是什么？", []),
    ("最喜欢的传统节日", "中秋节", "中国的传统节日里，我最喜欢哪个？", []),
    ("高中所在的学校", "青岚中学", "我在哪所学校读的高中？", []),
    ("最喜欢的棋类", "围棋", "我最爱下什么棋？", []),
    ("祖父教我的拿手菜", "糖醋莲藕", "我爷爷教我做的那道拿手菜叫什么？", []),
]
for i, (name, answer, paraphrase, aliases) in enumerate(traits):
    key = override(i + 1, 0, f"我的{name}是{answer}，这是我的长期个人信息。",
                   1.0 if i < 6 else 0.5, "个人背景")
    case("长期事实", f"我的{name}是什么？", [answer], [key], aliases)
    case("同义表达", paraphrase, [answer], [key], aliases)

for i in range(12):
    day = 40 + i * 13
    names = ["翠湖", "雪峰", "青溪", "白沙", "竹里", "松风",
             "星湾", "云岭", "西桥", "望海", "花田", "月港"]
    answer = names[i] + "客栈"
    key = override(day, 1, f"我第一次参加{names[i]}徒步活动，晚上住在{answer}。",
                   topic="旅行")
    case("早年事件", f"我第一次参加{names[i]}徒步时，住在哪家客栈？", [answer], [key])

for i in range(12):
    day = 210 + i * 7
    answer = ["木兰花", "水仙花", "山茶花", "栀子花"][i % 4]
    date = START + dt.timedelta(days=day)
    key = override(day, 2, f"我买来一盆{answer}作为周末的小奖励。", topic="园艺")
    case("指定日期", f"{date.year}年{date.month}月{date.day}日，我买了什么花？",
         [answer], [key])

settings = [
    ("常用键盘", ["雪松键盘", "竹影键盘", "海盐键盘"]),
    ("上班交通方式", ["公交车", "自行车", "地铁"]),
    ("常住城市", ["苏州", "杭州", "宁波"]),
    ("喜欢的饮品", ["红茶", "乌龙茶", "茉莉花茶"]),
    ("常用笔记软件", ["轻笺", "云页", "墨匣"]),
    ("手机品牌", ["星耀", "青禾", "晨光"]),
    ("周末固定运动", ["慢跑", "游泳", "羽毛球"]),
    ("常用邮箱服务", ["松果邮箱", "晴空邮箱", "白鹭邮箱"]),
    ("常用耳机", ["松风耳机", "月河耳机", "星野耳机"]),
    ("喜欢的冰淇淋口味", ["香草", "抹茶", "开心果"]),
    ("工作的项目名称", ["溪流计划", "山谷计划", "灯塔计划"]),
    ("通常入睡的时间", ["晚上十点", "晚上十一点", "晚上十点半"]),
]
for i, (name, values) in enumerate(settings):
    versions = []
    for phase, day in enumerate((300 + i, 1000 + i, 1810 + i)):
        key = override(day, 3, f"从今天开始，我的{name}改为{values[phase]}，以这次更新为准。",
                       topic="习惯变更")
        versions.append({"day": day, "key": key, "answer": values[phase]})
    case("最新变更", f"我现在的{name}是什么？", [values[-1]], [versions[-1]["key"]])
    cases[-1]["versions"] = versions

for i in range(12):
    title = ["春山录", "夏溪录", "秋桥录", "冬港录", "花田录", "竹影录",
             "雪松录", "海盐录", "月河录", "晴空录", "青禾录", "晨光录"][i]
    answer = ["靛蓝", "浅紫", "橘黄", "墨绿"][i % 4]
    key = override(100 + i * 3, 4, f"我买的《{title}》封面是{answer}色，价格四十二元。",
                   topic="书籍")
    override(120 + i * 3, 5, f"我在店里看过《{title}续篇》，封面是玫红色，但没有买。",
             topic="书籍")
    case("相似事件", f"我买的《{title}》是什么颜色的封面？", [answer], [key],
         [answer + "色"])

for i in range(12):
    name = ["林舟", "沈棠", "顾宁", "许川", "周禾", "江月",
            "季青", "陆遥", "宋桥", "唐松", "叶澄", "温岚"][i]
    fruit = ["芒果", "草莓", "蓝莓", "菠萝"][i % 4]
    weekday = ["周一", "周二", "周三", "周四"][i % 4]
    key1 = override(65 + i * 4, 6, f"我的朋友{name}最喜欢的水果是{fruit}。", topic="朋友")
    key2 = override(67 + i * 4, 7, f"我和{name}约定每{weekday}一起去游泳。", topic="朋友")
    case("跨条综合", f"朋友{name}喜欢什么水果？我和他约定每周哪天游泳？",
         [fruit, weekday], [key1, key2])

for i, (question, text) in enumerate([
    ("我买的云帆相机是什么型号？", "我买的云帆相机型号是E7。"),
    ("我的蓝溪会员卡是哪年办的？", "我的蓝溪会员卡是2023年办的。"),
    ("我的白鹭自行车是几速的？", "我的白鹭自行车是二十一速的。"),
    ("我养的兔子叫什么？", "我养的兔子叫雪球。"),
    ("我的金鱼是什么品种？", "我的金鱼品种是兰寿。"),
    ("我最喜欢的歌手是谁？", "我最喜欢的歌手是陈粒。"),
    ("我第一次出国去了哪里？", "我第一次出国去了越南。"),
    ("我的星河相机是什么颜色？", "我的星河相机是银灰色的。"),
    ("我的青谷健身教练叫什么？", "我的青谷健身教练叫陈远。"),
    ("我的云杉咖啡机是什么品牌？", "我的云杉咖啡机品牌是山岚。"),
    ("我常吃的那家面包店叫什么？", "我常吃的那家面包店叫麦田。"),
    ("我学西班牙语的老师是谁？", "我学西班牙语的老师是罗青。"),
]):
    records.append({"key": f"foreign{i:03d}", "day": 1825, "text": text,
                    "importance": 1.0, "owner": "B", "topic": "他人信息"})
    case("账号隔离", question, ["未知"], [])

for i, item in enumerate([
    "我最喜欢的歌剧是哪一部？", "我的滑雪板是什么品牌？", "我养的鹦鹉叫什么？",
    "我小时候住的街道叫什么？", "我最喜欢的天文学家是谁？", "我第一次登山的海拔是多少？",
    "我的第一台游戏机是什么型号？", "我最喜欢的雕塑作品是哪件？", "我想申请哪所海外大学？",
    "我参加的合唱团叫什么？", "我的第一张黑胶唱片是什么专辑？", "我最喜欢的火车路线是哪条？",
]):
    case("无答案问题", item, ["未知"], [])

for i in range(288):
    row = dict(records[(i * 61) % (DAYS * 10)])
    row.update(key=f"foreign-noise{i:03d}", owner="B", importance=1.0, day=1825)
    records.append(row)

dataset = {
    "version": "lm-fiveyear-20261106-v1", "seed": 20261106,
    "start": START.isoformat(), "end_exclusive": END.isoformat(),
    "days": DAYS, "daily_memories": 10, "principal_memories": DAYS * 10,
    "foreign_memories": len(records) - DAYS * 10,
    "horizons": [
        {"label": "1年", "days": (dt.date(2022, 11, 6)-START).days},
        {"label": "3年", "days": (dt.date(2024, 11, 6)-START).days},
        {"label": "5年", "days": DAYS},
    ],
    "rubric": {
        "hit1": "首条命中任一标准答案来源；综合题首条只可能包含部分来源。",
        "hit5": "前五条包含全部标准答案来源；无答案题不纳入召回率。",
        "answer": "模型JSON answer字段与冻结答案/预置别名严格匹配；综合题须两项均对。",
        "grounding": "正例引用覆盖全部答案来源且都来自实际召回；无答案题回答未知且不引用。",
        "thresholds": {"positive_hit5_warn_below": 0.9, "answer_warn_below": 0.9,
                       "identity_leak_block_above": 0},
    },
    "records": records, "cases": cases,
}
payload = json.dumps(dataset, ensure_ascii=False, indent=2).encode()
(ROOT / "dataset.json").write_bytes(payload)
print(json.dumps({"days": DAYS, "principal": DAYS*10, "foreign": len(records)-DAYS*10,
                  "cases": len(cases), "sha256": hashlib.sha256(payload).hexdigest()}))
