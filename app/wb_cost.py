import math
import re

# 官方价格按每百万 Token 计费；deepseek-v4.1-flash 取官方高峰时段价格。
UNIT = 1000000.0
MAX_MODELS = 200
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
PRICE_KEYS = ("input", "cached_input", "output")

DEFAULT_MODELS = (
    {"model": "deepseek-v4.1-flash", "input": 2.0, "cached_input": 0.04, "output": 8.0},
)


def _default_config():
    return {"credit_price": 0.0, "models": [dict(row) for row in DEFAULT_MODELS]}


def _price(value, label, fallback=0.0):
    """把一项价格读成不小于 0 的有限数；空值取 fallback。"""
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        raise ValueError("%s must be a number" % label)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number" % label)
    if not math.isfinite(number) or number < 0:
        raise ValueError("%s must be a non-negative number" % label)
    return number


def _row(item):
    """一行模型价格；模型名无效时返回 None，价格非法时抛 ValueError。"""
    if not isinstance(item, dict):
        raise ValueError("each cost estimate model must be an object")
    model = str(item.get("model") or "").strip()
    input_price = _price(item.get("input"), "input")
    cached_raw = item.get("cached_input")
    cached_price = (input_price if cached_raw in (None, "")
                    else _price(cached_raw, "cached_input"))
    output_price = _price(item.get("output"), "output")
    prices = {"input": input_price, "cached_input": cached_price,
              "output": output_price}
    if not model:
        if any(prices.values()):
            raise ValueError("a cost estimate row is missing its model")
        return None
    if not MODEL_RE.match(model):
        raise ValueError(
            "model id must start with a letter or digit and use letters, digits, "
            "dot, dash, colon, underscore or slash")
    return {"model": model, **prices}


def clean(raw):
    """校验面板提交的费用估算设置，返回可以写进 settings.json 的形状。

    空模型名且价格全为 0 的行直接丢掉；模型名重复、价格非法或形状不对时抛
    ValueError，由调用方转成 400。价格全为 0 的行表示不参与估算，同样不存储。
    """
    if not isinstance(raw, dict):
        raise ValueError("cost_estimate must be an object")
    credit_price = _price(raw.get("credit_price"), "credit_price")
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise ValueError("cost_estimate.models must be a list")
    if len(rows) > MAX_MODELS:
        raise ValueError("cost_estimate.models can hold at most %d rows" % MAX_MODELS)
    out = []
    seen = set()
    for item in rows:
        row = _row(item)
        if row is None:
            continue
        key = row["model"].lower()
        if key in seen:
            raise ValueError("duplicate model in cost estimate: %s" % row["model"])
        seen.add(key)
        if not any(row[field] for field in PRICE_KEYS):
            continue
        out.append(row)
    return {"credit_price": credit_price, "models": out}


def normalise(raw):
    """读取磁盘上的设置；缺失或损坏时回到内置默认价格表。

    空列表是有效配置：用户显式删掉全部模型后，没有模型参与估算。
    """
    if not isinstance(raw, dict):
        return _default_config()
    credit_price = 0.0
    try:
        credit_price = _price(raw.get("credit_price"), "credit_price")
    except ValueError:
        credit_price = 0.0
    rows = raw.get("models")
    if not isinstance(rows, list):
        return _default_config()
    out = []
    seen = set()
    for item in rows[:MAX_MODELS]:
        try:
            row = _row(item)
        except ValueError:
            continue
        if row is None:
            continue
        key = row["model"].lower()
        if key in seen or not any(row[field] for field in PRICE_KEYS):
            continue
        seen.add(key)
        out.append(row)
    return {"credit_price": credit_price, "models": out}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def estimate(by_model, config):
    """按官方价格折算每个模型的费用，并对比积分折算的支出。

    `by_model` 是用量快照的 by_model 桶；`config` 是 normalise() 的结果。
    没有价格（或三项价格全为 0）的模型 priced=False，金额按 0 计，不进入合计。
    缓存命中价已经按剩余输入算，命中 token 不会重复按未命中价计费。
    """
    config = config if isinstance(config, dict) else _default_config()
    credit_price = _number(config.get("credit_price"))
    prices = {}
    for row in config.get("models") or []:
        if isinstance(row, dict) and row.get("model"):
            prices[row["model"]] = row
    rows = []
    official_total = paid_total = credit_total = 0.0
    priced_models = unpriced_models = requests_total = 0
    unpriced = []
    for model in sorted(by_model or {}):
        bucket = by_model.get(model) or {}
        prompt = _number(bucket.get("prompt_tokens"))
        cached = min(_number(bucket.get("cached_tokens")), prompt)
        output = _number(bucket.get("completion_tokens"))
        credit = _number(bucket.get("credit"))
        requests = int(_number(bucket.get("requests")))
        price = prices.get(model)
        priced = price is not None and any(
            _number(price.get(field)) for field in PRICE_KEYS)
        official = paid = 0.0
        if priced:
            official = ((prompt - cached) * _number(price.get("input"))
                        + cached * _number(price.get("cached_input"))
                        + output * _number(price.get("output"))) / UNIT
            paid = credit * credit_price
            official_total += official
            paid_total += paid
            credit_total += credit
            requests_total += requests
            priced_models += 1
        else:
            unpriced_models += 1
            unpriced.append(model)
        rows.append({
            "model": model,
            "requests": requests,
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "completion_tokens": output,
            "total_tokens": _number(bucket.get("total_tokens")),
            "credit": credit,
            "priced": priced,
            "prices": ({"input": _number(price.get("input")),
                        "cached_input": _number(price.get("cached_input")),
                        "output": _number(price.get("output"))}
                       if priced else None),
            "official_cny": official,
            "paid_cny": paid,
            "saved_cny": official - paid if priced else 0.0,
        })
    rows.sort(key=lambda row: (not row["priced"], -row["official_cny"], row["model"]))
    return {
        "credit_price": credit_price,
        "models": rows,
        "unpriced_models": unpriced,
        "summary": {
            "official_cny": official_total,
            "paid_cny": paid_total,
            "saved_cny": official_total - paid_total,
            "credit": credit_total,
            "requests": requests_total,
            "priced_models": priced_models,
            "unpriced_models": unpriced_models,
        },
    }
