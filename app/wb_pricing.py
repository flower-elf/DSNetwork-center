"""网关请求按 OpenRouter 挂牌价估算的等效成本，不是实际扣费。

口径：模型级价目（USD/token，非最便宜供应商），按快照 usd_cny 折算为 CNY，每 `unit`（1M）token 计价；
条件定价的模型用区间（输入超阈值高价、UTC 时段费率），由行长度与时间戳选定。
快照由 `_fetch_pricing.py` 生成，pricing/pricing.json 或 WB_PRICING_FILE 覆盖内联副本。
PriceRefresher 按内容哈希存逐模型策略与时间线，见过的价格复用，无人引用的策略被清理。
上游新增模型无需发版：抓取输入含实时目录新增，请求路径不联网；只依赖标准库。
"""
import bisect
import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.request

import wb_host

# 快照由 _fetch_pricing.py 生成，来源与刷新步骤见该脚本。内嵌副本是每个
# 安装（含 Docker 镜像）都有的回退价表。
_JSON = r'''
{
  "meta": {
    "generated_at": "2026-10-02T01:39:39",
    "base_currency": "CNY",
    "usd_cny": 7.1,
    "sources": {
      "openrouter": "https://openrouter.ai/api/v1/models"
    },
    "note": "单价均为「每百万 token」的美元价，取自 OpenRouter 公布的模型级价（同一模型可能由多家 provider 承接，取它默认路由那家的公布价，不是各家最低价）；按 usd_cny 折算成人民币；按条件定价的模型（输入长度阈值或 UTC 时段）另带 bands，按每条请求的输入长度与时间取档。OpenRouter 的缓存写入价与音频价，本地 usage 日志没有对应的 token 数，未计入。"
  },
  "models": {
    "deepseek-v4.1-flash": {
      "display": "deepseek-v4.1-flash",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-v4.1-flash",
      "flat": {
        "input_cache_hit": 0.01,
        "input_cache_miss": 0.03,
        "output": 0.5
      }
    },
    "gpt-6-astra": {
      "display": "gpt-6-astra",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-6-astra",
      "flat": {
        "input_cache_hit": 1.0,
        "input_cache_miss": 10.0,
        "output": 50.0
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 2.0,
            "input_cache_miss": 20.0,
            "output": 75.0
          },
          "min_prompt_tokens": 272000
        }
      ]
    },
    "hy4-preview-f": {
      "display": "hy4-preview-f",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hy4-preview",
      "flat": {
        "input_cache_hit": 0.0378,
        "input_cache_miss": 0.7506,
        "output": 2.2509
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.042,
            "input_cache_miss": 0.834,
            "output": 2.501
          },
          "days": null,
          "start": 0,
          "end": 1600
        },
        {
          "flat": {
            "input_cache_hit": 0.0378,
            "input_cache_miss": 0.7506,
            "output": 2.2509
          },
          "days": null,
          "start": 1600,
          "end": 2400
        }
      ]
    },
    "hy4-preview": {
      "display": "hy4-preview",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hy4-preview",
      "flat": {
        "input_cache_hit": 0.0378,
        "input_cache_miss": 0.7506,
        "output": 2.2509
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.042,
            "input_cache_miss": 0.834,
            "output": 2.501
          },
          "days": null,
          "start": 0,
          "end": 1600
        },
        {
          "flat": {
            "input_cache_hit": 0.0378,
            "input_cache_miss": 0.7506,
            "output": 2.2509
          },
          "days": null,
          "start": 1600,
          "end": 2400
        }
      ]
    },
    "hy3": {
      "display": "hy3",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hy3",
      "flat": {
        "input_cache_hit": 0.020625,
        "input_cache_miss": 0.0825,
        "output": 0.33
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.033,
            "input_cache_miss": 0.132,
            "output": 0.528
          },
          "days": null,
          "start": 0,
          "end": 1600
        },
        {
          "flat": {
            "input_cache_hit": 0.020625,
            "input_cache_miss": 0.0825,
            "output": 0.33
          },
          "days": null,
          "start": 1600,
          "end": 2400
        }
      ]
    },
    "gpt-5.6-sol": {
      "display": "gpt-5.6-sol",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-5.6-sol",
      "flat": {
        "input_cache_hit": 0.2,
        "input_cache_miss": 2.0,
        "output": 10.0
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.4,
            "input_cache_miss": 4.0,
            "output": 15.0
          },
          "min_prompt_tokens": 272000
        }
      ]
    },
    "gpt-5.6-terra": {
      "display": "gpt-5.6-terra",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-5.6-terra",
      "flat": {
        "input_cache_hit": 0.2,
        "input_cache_miss": 2.0,
        "output": 12.0
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.4,
            "input_cache_miss": 4.0,
            "output": 18.0
          },
          "min_prompt_tokens": 272000
        }
      ]
    },
    "gpt-5.6-luna": {
      "display": "gpt-5.6-luna",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-5.6-luna",
      "flat": {
        "input_cache_hit": 0.02,
        "input_cache_miss": 0.2,
        "output": 1.2
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.04,
            "input_cache_miss": 0.4,
            "output": 1.8
          },
          "min_prompt_tokens": 272000
        }
      ]
    },
    "gpt-5.5": {
      "display": "gpt-5.5",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-5.5",
      "flat": {
        "input_cache_hit": 0.5,
        "input_cache_miss": 5.0,
        "output": 30.0
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 1.0,
            "input_cache_miss": 10.0,
            "output": 45.0
          },
          "min_prompt_tokens": 272000
        }
      ]
    },
    "gpt-5.4": {
      "display": "gpt-5.4",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-5.4",
      "flat": {
        "input_cache_hit": 0.25,
        "input_cache_miss": 2.5,
        "output": 15.0
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.5,
            "input_cache_miss": 5.0,
            "output": 22.5
          },
          "min_prompt_tokens": 272000
        }
      ]
    },
    "grok-4.7": {
      "display": "grok-4.7",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "x-ai/grok-4.7",
      "flat": {
        "input_cache_hit": 0.5,
        "input_cache_miss": 2.0,
        "output": 6.0
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 1.0,
            "input_cache_miss": 4.0,
            "output": 12.0
          },
          "min_prompt_tokens": 200000
        }
      ]
    },
    "gpt-5.3-codex": {
      "display": "gpt-5.3-codex",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "openai/gpt-5.3-codex",
      "flat": {
        "input_cache_hit": 0.175,
        "input_cache_miss": 1.75,
        "output": 14.0
      }
    },
    "gemini-3.5-flash": {
      "display": "gemini-3.5-flash",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "google/gemini-3.5-flash",
      "flat": {
        "input_cache_hit": 0.15,
        "input_cache_miss": 1.5,
        "output": 9.0
      }
    },
    "glm-5.3": {
      "display": "glm-5.3",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-5.3",
      "flat": {
        "input_cache_hit": 0.192,
        "input_cache_miss": 0.24,
        "output": 3.39
      }
    },
    "glm-5.3-flash": {
      "display": "glm-5.3-flash",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-5.3-flash",
      "flat": {
        "input_cache_hit": 0.03,
        "input_cache_miss": 0.15,
        "output": 0.5
      }
    },
    "glm-5.2": {
      "display": "glm-5.2",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-5.2",
      "flat": {
        "input_cache_hit": 0.26,
        "input_cache_miss": 0.41,
        "output": 3.99
      }
    },
    "kimi-k3": {
      "display": "kimi-k3",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "moonshotai/kimi-k3",
      "flat": {
        "input_cache_hit": 0.6635,
        "input_cache_miss": 0.6635,
        "output": 10.0
      }
    },
    "kimi-k2.6": {
      "display": "kimi-k2.6",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "moonshotai/kimi-k2.6",
      "flat": {
        "input_cache_hit": 0.07312,
        "input_cache_miss": 0.43415,
        "output": 1.828
      }
    },
    "hy3-x": {
      "display": "hy3-x",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hy3",
      "flat": {
        "input_cache_hit": 0.020625,
        "input_cache_miss": 0.0825,
        "output": 0.33
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.033,
            "input_cache_miss": 0.132,
            "output": 0.528
          },
          "days": null,
          "start": 0,
          "end": 1600
        },
        {
          "flat": {
            "input_cache_hit": 0.020625,
            "input_cache_miss": 0.0825,
            "output": 0.33
          },
          "days": null,
          "start": 1600,
          "end": 2400
        }
      ]
    },
    "hy4-preview-dev": {
      "display": "hy4-preview-dev",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hy4-preview",
      "flat": {
        "input_cache_hit": 0.0378,
        "input_cache_miss": 0.7506,
        "output": 2.2509
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.042,
            "input_cache_miss": 0.834,
            "output": 2.501
          },
          "days": null,
          "start": 0,
          "end": 1600
        },
        {
          "flat": {
            "input_cache_hit": 0.0378,
            "input_cache_miss": 0.7506,
            "output": 2.2509
          },
          "days": null,
          "start": 1600,
          "end": 2400
        }
      ]
    },
    "hy4-preview-x": {
      "display": "hy4-preview-x",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hy4-preview",
      "flat": {
        "input_cache_hit": 0.0378,
        "input_cache_miss": 0.7506,
        "output": 2.2509
      },
      "bands": [
        {
          "flat": {
            "input_cache_hit": 0.042,
            "input_cache_miss": 0.834,
            "output": 2.501
          },
          "days": null,
          "start": 0,
          "end": 1600
        },
        {
          "flat": {
            "input_cache_hit": 0.0378,
            "input_cache_miss": 0.7506,
            "output": 2.2509
          },
          "days": null,
          "start": 1600,
          "end": 2400
        }
      ]
    },
    "minimax-m2.5": {
      "display": "minimax-m2.5",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "minimax/minimax-m2.5",
      "flat": {
        "input_cache_hit": 0.027,
        "input_cache_miss": 0.27,
        "output": 1.08
      }
    },
    "glm-5v-turbo": {
      "display": "glm-5v-turbo",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-5v-turbo",
      "flat": {
        "input_cache_hit": 0.24,
        "input_cache_miss": 1.2,
        "output": 4.0
      }
    },
    "glm-5.1": {
      "display": "glm-5.1",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-5.1",
      "flat": {
        "input_cache_hit": 0.17914,
        "input_cache_miss": 0.9646,
        "output": 3.0316
      }
    },
    "glm-5.0-turbo": {
      "display": "glm-5.0-turbo",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-5-turbo",
      "flat": {
        "input_cache_hit": 0.24,
        "input_cache_miss": 1.2,
        "output": 4.0
      }
    },
    "glm-4.6v": {
      "display": "glm-4.6v",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-4.6v",
      "flat": {
        "input_cache_hit": 0.05,
        "input_cache_miss": 0.3,
        "output": 0.9
      }
    },
    "kimi-k3-1": {
      "display": "kimi-k3-1",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "moonshotai/kimi-k3",
      "flat": {
        "input_cache_hit": 0.6635,
        "input_cache_miss": 0.6635,
        "output": 10.0
      }
    },
    "kimi-k2.7": {
      "display": "kimi-k2.7",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "moonshotai/kimi-k2.7-code",
      "flat": {
        "input_cache_hit": 0.18,
        "input_cache_miss": 0.6712,
        "output": 3.35
      }
    },
    "kimi-k2.5": {
      "display": "kimi-k2.5",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "moonshotai/kimi-k2.5",
      "flat": {
        "input_cache_hit": 0.07,
        "input_cache_miss": 0.45,
        "output": 2.25
      }
    },
    "kimi-k2-thinking": {
      "display": "kimi-k2-thinking",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "moonshotai/kimi-k2-thinking",
      "flat": {
        "input_cache_hit": null,
        "input_cache_miss": 0.6,
        "output": 2.5
      }
    },
    "minimax-m3": {
      "display": "minimax-m3",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "minimax/minimax-m3",
      "flat": {
        "input_cache_hit": 0.06,
        "input_cache_miss": 0.3,
        "output": 1.2
      }
    },
    "minimax-m2.7": {
      "display": "minimax-m2.7",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "minimax/minimax-m2.7",
      "flat": {
        "input_cache_hit": 0.042,
        "input_cache_miss": 0.21,
        "output": 0.84
      }
    },
    "glm-4.6": {
      "display": "glm-4.6",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "z-ai/glm-4.6",
      "flat": {
        "input_cache_hit": 0.08,
        "input_cache_miss": 0.43,
        "output": 1.75
      }
    },
    "deepseek-v4-flash": {
      "display": "deepseek-v4-flash",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-v4-flash",
      "flat": {
        "input_cache_hit": 0.008372,
        "input_cache_miss": 0.04186,
        "output": 0.08372
      }
    },
    "deepseek-v4-pro": {
      "display": "deepseek-v4-pro",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-v4-pro",
      "flat": {
        "input_cache_hit": 0.0174,
        "input_cache_miss": 0.2088,
        "output": 0.4176
      }
    },
    "deepseek-v3-2-volc": {
      "display": "deepseek-v3-2-volc",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-v3.2",
      "flat": {
        "input_cache_hit": 0.028,
        "input_cache_miss": 0.28,
        "output": 0.42
      }
    },
    "deepseek-v3-1-volc": {
      "display": "deepseek-v3-1-volc",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-chat-v3.1",
      "flat": {
        "input_cache_hit": 0.13,
        "input_cache_miss": 0.25,
        "output": 0.95
      }
    },
    "deepseek-v3-1": {
      "display": "deepseek-v3-1",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-chat-v3.1",
      "flat": {
        "input_cache_hit": 0.13,
        "input_cache_miss": 0.25,
        "output": 0.95
      }
    },
    "deepseek-r1-0528": {
      "display": "deepseek-r1-0528",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-r1-0528",
      "flat": {
        "input_cache_hit": 0.35,
        "input_cache_miss": 0.5,
        "output": 2.15
      }
    },
    "deepseek-v3-0324": {
      "display": "deepseek-v3-0324",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "deepseek/deepseek-chat-v3-0324",
      "flat": {
        "input_cache_hit": 0.11,
        "input_cache_miss": 0.29,
        "output": 1.14
      }
    },
    "hunyuan-2.0-instruct": {
      "display": "hunyuan-2.0-instruct",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hunyuan-a13b-instruct",
      "flat": {
        "input_cache_hit": null,
        "input_cache_miss": 0.14,
        "output": 0.57
      }
    },
    "hunyuan-chat": {
      "display": "hunyuan-chat",
      "source": "openrouter",
      "currency": "USD",
      "unit": 1000000,
      "or_id": "tencent/hunyuan-a13b-instruct",
      "flat": {
        "input_cache_hit": null,
        "input_cache_miss": 0.14,
        "output": 0.57
      }
    }
  }
}
'''

_INLINE = json.loads(_JSON)

_lock = threading.Lock()
_file_cache = {"key": None, "data": None}


def _candidate_file():
    """带外快照路径；未配置时为 None。"""
    env = os.environ.get("WB_PRICING_FILE")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "pricing", "pricing.json")
    return path if os.path.isfile(path) else None


def load_pricing():
    """文件（存在时）优先于内嵌副本；内嵌副本保证文件损坏或缺失时定价不中断。"""
    path = _candidate_file()
    key = ("inline", 0)
    if path:
        try:
            key = (path, os.path.getmtime(path))
        except OSError:
            path = None
            key = ("inline", 0)
    with _lock:
        if _file_cache["key"] == key and _file_cache["data"] is not None:
            return _file_cache["data"]
    data = None
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and isinstance(loaded.get("models"), dict):
                data = loaded
        except Exception:
            data = None
    if data is None:
        data = _INLINE
    with _lock:
        _file_cache.update({"key": key, "data": data})
    return data


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _utc_parts(epoch_sec):
    """UTC 的（星期, HHMM），即价源标注时段所用的时区；查找不得依赖宿主时钟的时区。"""
    t = time.gmtime(_as_float(epoch_sec))
    return _WEEKDAYS[t.tm_wday], t.tm_hour * 100 + t.tm_min


def _band_applies(band, prompt_tokens, day, hhmm):
    """档位条件二选一：最短输入长度，或 UTC 时间窗；同一价源条目里两者不混用。"""
    threshold = band.get("min_prompt_tokens")
    if threshold is not None:
        return prompt_tokens >= _as_float(threshold)
    days = band.get("days")
    if days and day not in days:
        return False
    start = _as_float(band.get("start"))
    end = _as_float(band.get("end")) or 2400.0
    return start <= hhmm < end


def band_for(entry, epoch_sec, prompt_tokens=0.0):
    """这一行的价目表与档位序号：条件档按源数据顺序保留，取最后一条满足条件的（长度档按阈值升序即
    最紧档，时段档互不重叠）；没有条件价、或这一行哪档都不满足时回落到模型自己的单一价（不会变成
    0），此时档序号为 None。"""
    bands = entry.get("bands")
    if isinstance(bands, list) and bands:
        day, hhmm = _utc_parts(epoch_sec)
        hit = None
        for index, band in enumerate(bands):
            if not isinstance(band, dict):
                continue
            if not _band_applies(band, prompt_tokens, day, hhmm):
                continue
            table = band.get("flat")
            if isinstance(table, dict):
                hit = (table, index)
        if hit:
            return hit
    table = entry.get("flat")
    return (table if isinstance(table, dict) else {}), None


def _rate_for(entry, epoch_sec, prompt_tokens, meta):
    table, band = band_for(entry, epoch_sec, prompt_tokens)
    factor = 1.0
    if str(entry.get("currency") or "").upper() == "USD":
        factor = _as_float(meta.get("usd_cny"), 1.0) or 1.0
    return table, band, factor


def _pick(rate, key, fallback=None):
    """缺失时回退到保守的相邻项；没有命中缓存价的行按未命中价计（绝不低于参考引擎），估算不会少算。"""
    value = rate.get(key)
    if value is None and fallback:
        value = rate.get(fallback)
    return _as_float(value)


def compute_row(row, pricing=None):
    """一行 usage 的估算成本，单位 CNY。返回 {"cny", "known", "band", "rates"}：known=False 表示无价目，
    cny 保持 0，不猜数；band 是时间戳落到的时段档序号（单一价为 None），rates 是该档每百万 token
    原样价目。"""
    if not row:
        return {"cny": 0.0, "known": False, "band": None, "rates": None}
    pricing = pricing if pricing is not None else load_pricing()
    entry = (pricing.get("models") or {}).get(row.get("model"))
    if not isinstance(entry, dict):
        return {"cny": 0.0, "known": False, "band": None, "rates": None}
    meta = pricing.get("meta") or {}
    unit = _as_float(entry.get("unit")) or 1000000.0
    prompt = _as_float(row.get("prompt_tokens"))
    rate, band, factor = _rate_for(entry, row.get("at"), prompt, meta)
    cached = min(_as_float(row.get("cached_tokens")), prompt)
    uncached = prompt - cached
    # output 单价只按 completion_tokens 计：上游把 reasoning token 算在 completion 里（线上与日志
    # 行都验证过，见 tests/_test_pricing.py），缺 usage 块时估算同口径；再计一次 reasoning 就是重复收费。
    output = _as_float(row.get("completion_tokens"))
    miss = _pick(rate, "input_cache_miss")
    hit = _pick(rate, "input_cache_hit", "input_cache_miss")
    out_rate = _pick(rate, "output")
    cny = (uncached * miss + cached * hit + output * out_rate) / unit * factor
    return {"cny": cny, "known": True, "band": band, "rates": rate}


def sum_rows(rows, pricing=None):
    """批量合计：{"cny", "priced", "missing"}；missing 是快照无法定价的行，按模型 id 记出现次数，避免悄悄少报。"""
    pricing = pricing if pricing is not None else load_pricing()
    total = 0.0
    priced = 0
    missing = {}
    for row in rows or []:
        cost = compute_row(row, pricing=pricing)
        if cost["known"]:
            total += cost["cny"]
            priced += 1
        else:
            mid = str((row or {}).get("model") or "")
            missing[mid] = missing.get(mid, 0) + 1
    return {"cny": total, "priced": priced, "missing": missing}


def usd_cny():
    try:
        return float((load_pricing().get("meta") or {}).get("usd_cny") or 1.0)
    except (TypeError, ValueError):
        return 1.0


# ---- 快照抓取 ------------------------------------------------------------
# 同一条代码路径由命令行刷新器（_fetch_pricing.py）与下面的进程内刷新共用，
# 两份入口不会产生差异。

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
USD_CNY = 7.10

# 除内置快照外，这些区域的在线目录也参与价格输入；fetch_remote_product_config() 按区域抓取
# （只认 intl/cn），两个都要：任一区域列出的模型都必须能定价。
LIVE_CATALOG_REALMS = ("intl", "cn")

# 与 OpenRouter 写法不同、自动匹配找不到的名字。刻意保持简短：按名字能匹配
# 上的模型不得列进来，否则本表会变成它要解决的维护负担。
OVERRIDES = {
    # hub 名字比 OpenRouter 多/少一个后缀
    "hy4-preview-f": "tencent/hy4-preview",
    "hy4-preview-dev": "tencent/hy4-preview",
    "hy4-preview-x": "tencent/hy4-preview",
    "hy3-x": "tencent/hy3",
    "kimi-k3-1": "moonshotai/kimi-k3",
    "kimi-k2.7": "moonshotai/kimi-k2.7-code",
    # hub 名字与 OpenRouter 的写法不同（版本号/代号差异）
    "glm-5.0-turbo": "z-ai/glm-5-turbo",
    "deepseek-v3-1": "deepseek/deepseek-chat-v3.1",
    "deepseek-v3-1-volc": "deepseek/deepseek-chat-v3.1",
    "deepseek-v3-2-volc": "deepseek/deepseek-v3.2",
    "deepseek-v3-0324": "deepseek/deepseek-chat-v3-0324",
    # 混元系列在 OpenRouter 上是 a13b-instruct 这一个条目
    "hunyuan-2.0-instruct": "tencent/hunyuan-a13b-instruct",
    "hunyuan-chat": "tencent/hunyuan-a13b-instruct",
}

# 变体后缀表：剥掉后缀按基名重走完整解析链（手填覆盖 → 源码覆盖表 → 归一化全等唯一），仍要求
# 唯一命中，否则照旧算未定价。第二列表示是否参与自动剥离，只放确认过同一实体的后缀；不剥离的
# 后缀各有独立语义（-f 免费/付费档、-dev 另一份构建、-x 单价不同），关闭变体继承开关后整体失效。
VARIANT_SUFFIXES = (
    ("-lkeap", True),   # 渠道后缀，能落到同名基准（部分经 OVERRIDES）
    ("-taiji", True),   # 发行后缀，剥掉后 OpenRouter 无同名基准，照样未定价
    ("-volc", True),    # 渠道后缀，OVERRIDES 里已有更精确的写法，兜底新变体
    ("-sg", True),      # 节点后缀，同样要求剥掉后唯一命中
    ("-f", False),      # 免费档标记，上游 curation 也当独立档位
    ("-dev", False),    # 预览档后缀，语义是另一份构建
    ("-x", False),      # 加量档后缀，继承会把价压低
)

# 面板手填覆盖的路径由 data_dir() 决定，见 runtime_overrides()。

# OpenRouter 用 UTC 星期名与 HHMM 表达时段档位。
WEEKDAY_KEYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday")
WEEKDAY_SHORT = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_NOTE = ("单价均为「每百万 token」的美元价，取自 OpenRouter 公布的模型级价"
         "（同一模型可能由多家 provider 承接，取它默认路由那家的公布价，"
         "不是各家最低价）；按 usd_cny 折算成人民币；按条件定价的模型"
         "（输入长度阈值或 UTC 时段）另带 bands，按每条请求的输入长度与"
         "时间取档。OpenRouter 的缓存写入价与音频价，本地 usage 日志没有"
         "对应的 token 数，未计入。")


def per_million(value):
    """OpenRouter 的单价是「每 token 的美元」，转成「每百万 token」。"""
    try:
        return round(float(value) * 1_000_000, 6)
    except (TypeError, ValueError):
        return None


def rates_from(item):
    return {
        "input_cache_hit": per_million(item.get("input_cache_read")),
        "input_cache_miss": per_million(item.get("prompt")),
        "output": per_million(item.get("completion")),
    }


def bands_from_overrides(or_entry):
    """条件档位列表；没有条件定价的模型返回空列表。

    OpenRouter 用 overrides 表达两种条件价，源数据里互不混用：
      - min_prompt_tokens：输入长度达到该阈值后改用的价；
      - utc_days / utc_start / utc_end：按时段（UTC）改用的价，HHMM 整数（1600 = 16:00），0 作终点表示当天结束。
    档位按源数组顺序保留，计价取最后一条满足条件的档（长度档按阈值升序，时段档互不重叠）。
    与基准价重复或不带条件的项不取；input_cache_write / audio / input_audio_cache 在本地
    usage 日志里没有对应 token 数，计不出价，不取。
    """
    out = []
    for item in or_entry.get("overrides") or []:
        if not isinstance(item, dict):
            continue
        band = {"flat": rates_from(item)}
        threshold = item.get("min_prompt_tokens")
        if threshold is not None:
            band["min_prompt_tokens"] = int(threshold)
        elif any(str(k).startswith("utc_") for k in item):
            days = item.get("utc_days")
            picked = [WEEKDAY_SHORT[WEEKDAY_KEYS.index(str(d).lower())]
                      for d in (days if isinstance(days, list) else [])
                      if str(d).lower() in WEEKDAY_KEYS]
            end = item.get("utc_end")
            band.update({
                "days": picked or None,
                "start": int(item.get("utc_start") or 0),
                "end": int(end) if end else 2400,
            })
        else:
            continue
        out.append(band)
    return out


def fetch_openrouter():
    """拉取 OpenRouter 模型列表，返回 {id: pricing}；网络失败时抛异常，由调用方决定记失败还是沿用旧价。"""
    req = urllib.request.Request(
        OPENROUTER_URL, headers={"User-Agent": "workbuddy2api-hub-pricing/1.0"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return {m.get("id"): (m.get("pricing") or {})
            for m in payload.get("data", [])}


def hub_model_ids():
    """内置目录里的模型 id（国际版在前，保序去重）——取价的静态基线。非 chat 的档位别名
    （default-model 等）匹配不到 OpenRouter，自然落进未定价清单；运行时取价还会并入 live 目录新增。"""
    import wb_catalog
    ids, seen = [], set()
    for source in (getattr(wb_catalog, "STATIC_INTL_MODELS", []),
                   getattr(wb_catalog, "STATIC_CN_MODELS", [])):
        for item in source or []:
            mid = str(item.get("id") or "").strip()
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(mid)
    return ids


def priced_model_ids(extra_ids=None):
    """取价输入清单：静态目录 ∪ 额外 id（去重保序，静态在前）。extra_ids 是 live 目录新增、静态快照
    还没有的模型，由调用方按 /v1/models 的同一套过滤整理好（见 PriceRefresher._live_ids）；别名不会
    从这里进来。"""
    ids, seen = [], set()
    for mid in hub_model_ids():
        if mid not in seen:
            seen.add(mid)
            ids.append(mid)
    for raw in extra_ids or []:
        mid = str(raw or "").strip()
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    return ids


# ---- 变体继承 ------------------------------------------------------------
# 名字对不上任何 OpenRouter 条目时，按 VARIANT_SUFFIXES 剥掉后缀、拿基名重走解析链；继承来的价
# 在策略里留 via / inherited_from，不来自同名条目。

_settings_dir_override = None
# 开关本身几乎不变，但 ensure_policy 会在每次「这个模型还没有价」的请求上走到
# 这里；按 (path, mtime, size) 缓存一次，省掉热路径上的 settings.json 解析。
_variant_cache = {"key": None, "value": True}
_variant_lock = threading.Lock()
# 总开关走同一套缓存：请求路径上每一行都要问一次「价估算开着吗」，更不能
# 每次去解析 settings.json。
_enabled_cache = {"key": None, "value": True}
_enabled_lock = threading.Lock()


def set_settings_dir(path):
    """wb_proxy 指向自己的 accounts 目录，让开关与面板读写同一份设置。"""
    global _settings_dir_override
    _settings_dir_override = path


# settings_path 每次把固定文件名 join 到 accounts 目录——现场逐行剖面里 os.path.join 排第二，
# 约三分之一就是这里；同一目录永远给同一字符串，按目录缓存一次即可，目录变了键就变。
_settings_path_cache = {}
_MEMO_LIMIT = 512


def _memo_put(cache, key, value):
    """只放由对象内容决定的派生值：清空只会让下一次多算一遍，正确性不依赖任何驱逐策略；并发下最坏
    是两个线程各算一遍，交回的是等价的两份。"""
    if len(cache) > _MEMO_LIMIT:
        cache.clear()
    cache[key] = value


def _cached_switch(cache, lock, reader, accounts_dir, default):
    """按 (path, mtime, size) 缓存，面板改设置会改 mtime、缓存自然失效；任何异常都退回 default，
    坏掉的 settings.json 不能把取价或请求路径带崩。"""
    try:
        import wb_settings
        path = accounts_dir or _settings_dir_override or os.environ.get("ACCOUNTS_DIR")
        if not path:
            return default
        try:
            settings = _settings_path_cache.get(path)
            if settings is None:
                settings = wb_settings.settings_path(path)
                _memo_put(_settings_path_cache, path, settings)
            # 一次 stat 同时取 mtime 与 size（getmtime + getsize 是两次系统调用）；缓存键就是
            # st_mtime 与 st_size，外部改设置当场可见——这个开关不能有 TTL。
            info = os.stat(settings)
            key = (settings, info.st_mtime, info.st_size)
        except OSError:
            key = (path, None, None)
        with lock:
            if cache["key"] == key:
                return cache["value"]
        value = reader(path)
        with lock:
            cache.update({"key": key, "value": value})
        return value
    except Exception:
        return default


def variant_inherit_enabled(accounts_dir=None):
    """默认打开；读不到设置也不影响取价。"""
    import wb_settings
    return _cached_switch(_variant_cache, _variant_lock,
                          wb_settings.pricing_variant_inherit, accounts_dir, True)


def pricing_enabled(accounts_dir=None):
    """默认打开；读不到设置按打开处理。"""
    import wb_settings
    return _cached_switch(_enabled_cache, _enabled_lock,
                          wb_settings.pricing_enabled, accounts_dir, True)


def variant_bases(hub_id):
    """按表内顺序返回基名；没启用剥离时为空。"""
    name = str(hub_id or "").strip()
    for suffix, enabled in VARIANT_SUFFIXES:
        if not enabled or not name.endswith(suffix):
            continue
        base = name[:-len(suffix)].strip()
        if base:
            yield base


def normalize(name):
    """名字归一化：小写、去掉所有非字母数字（- . _ 等一律忽略）。"""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def index_openrouter(or_models):
    """按归一化名字建索引，排除 :batch / :free 等变体。"""
    by_norm = {}
    for mid in or_models or {}:
        if ":" in mid:
            continue
        by_norm.setdefault(normalize(mid.split("/")[-1]), []).append(mid)
    return by_norm


def auto_match(hub_id, by_norm):
    """名字归一化后全等且唯一才返回，否则 None——宁可漏也不错。"""
    cands = by_norm.get(normalize(str(hub_id).split("/")[-1])) or []
    return cands[0] if len(cands) == 1 else None


def _resolve_once(hub_id, or_models, by_norm):
    """面板手填覆盖 → 源码覆盖表 → 归一化全等唯一，三层解析一次。"""
    for table in (runtime_overrides(), OVERRIDES):
        ref = table.get(hub_id)
        if ref and ref in or_models:
            return ref, True
    auto = auto_match(hub_id, by_norm)
    return (auto, False) if auto else (None, False)


def resolve(hub_id, or_models, by_norm, variants=None):
    """（OpenRouter id, 是否来自人工覆盖）；匹配不到返回 (None, False)。variants 为 None 时读设置开关
    （默认开）；剥后缀继承来的命中一律记作人工覆盖。"""
    if variants is None:
        variants = variant_inherit_enabled()
    ref, via_override = _resolve_once(hub_id, or_models, by_norm)
    if ref or not variants:
        return ref, via_override
    for base in variant_bases(hub_id):
        ref, via_override = _resolve_once(base, or_models, by_norm)
        if ref:
            return ref, True
    return None, False


def inherited_from(hub_id, or_models, by_norm):
    if _resolve_once(hub_id, or_models, by_norm)[0]:
        return None
    for base in variant_bases(hub_id):
        if _resolve_once(base, or_models, by_norm)[0]:
            return base
    return None


def entry_for(hub_id, ref, or_models, inherited=None, overridden=False):
    """一条 hub 模型 → OpenRouter 条目的快照记录（刷新与按需补价共用）。inherited 非空是剥后缀继承
    来的基准模型名，overridden 表示命中来自人工映射表；resolve() 的变体分支同样返回 via_override=True，
    两个分支必须靠 inherited 区分，否则变体继承会被误记成人工映射。via / inherited_from / override_from
    不参与 policy_id 的内容哈希，只作展示与追溯。"""
    p = or_models.get(ref) or {}
    entry = {
        "display": hub_id,
        "source": "openrouter",
        "currency": "USD",
        "unit": 1_000_000,
        "or_id": ref,
        "flat": rates_from(p),
    }
    bands = bands_from_overrides(p)
    if bands:
        entry["bands"] = bands
    if inherited:
        entry["via"] = "variant"
        entry["inherited_from"] = inherited
    elif overridden:
        entry["via"] = "override"
        entry["override_from"] = hub_id
    return entry


def build_snapshot(or_models, previous=None, extra_ids=None, variants=None):
    """(doc, unpriced, overridden)。or_models 为 None 表示这次抓取失败：沿用 previous 里的价（没有就
    留空），一次网络抖动不会把价格表清掉。extra_ids 是 live 目录新增、静态快照还没有的模型，与静态
    目录取并集后一起解析，去重保序。variants 覆盖变体继承开关（None = 读设置，默认开）。"""
    if variants is None:
        variants = variant_inherit_enabled()
    by_norm = index_openrouter(or_models) if or_models else {}
    models, unpriced, overridden = {}, [], []
    for hub_id in priced_model_ids(extra_ids):
        if or_models is None:
            old = (previous or {}).get(hub_id)
            if old:
                models[hub_id] = old
            else:
                unpriced.append(hub_id)
            continue
        ref, via_override = resolve(hub_id, or_models, by_norm, variants=variants)
        if not ref:
            unpriced.append(hub_id)
            continue
        if via_override:
            overridden.append(hub_id)
        models[hub_id] = entry_for(
            hub_id, ref, or_models,
            inherited=(inherited_from(hub_id, or_models, by_norm)
                       if variants else None),
            overridden=via_override)
    doc = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "base_currency": "CNY",
            "usd_cny": USD_CNY,
            "sources": {"openrouter": OPENROUTER_URL},
            "note": _NOTE,
        },
        "models": models,
    }
    return doc, unpriced, overridden


# ---- 价格策略 ------------------------------------------------------------
# 同一价格重复出现复用旧行：A -> B -> A 只写两行。

_policy_lock = threading.RLock()
_policies_cache = {"key": None, "data": None}
_timeline_cache = {"key": None, "data": None}
_data_dir_override = None


def data_dir():
    """默认是用量目录。"""
    if _data_dir_override:
        return _data_dir_override
    env = os.environ.get("WB_PRICING_DIR")
    if env:
        return env
    usage = os.environ.get("WB_PROXY_USAGE_DIR")
    return usage or os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage")


def set_data_dir(path):
    """wb_proxy 把这里指向它的数据目录，两边保持一致。"""
    global _data_dir_override
    with _policy_lock:
        _data_dir_override = path
        _policies_cache.update({"key": None, "data": None})
        _timeline_cache.update({"key": None, "data": None})


# 聚合两万行日志时每行都要问几次「策略表 / 时间线 / 覆盖表在哪」，每个问题都是一次 os.path.join。
# 四个派生路径按目录字符串缓存（键就是 data_dir() 本身），目录一变自然落到新键，结果与逐个 join 一致。
_paths_cache = {}


def _data_paths():
    """（策略表, 时间线, usage 日志, 覆盖表）：当前数据目录下的四个路径。"""
    base = data_dir()
    hit = _paths_cache.get(base)
    if hit is not None:
        return hit
    hit = (os.path.join(base, "pricing-policies.jsonl"),
           os.path.join(base, "pricing-timeline.jsonl"),
           os.path.join(base, "usage.jsonl"),
           os.path.join(base, "pricing-overrides.json"))
    _memo_put(_paths_cache, base, hit)
    return hit


def policies_path():
    return _data_paths()[0]


def timeline_path():
    return _data_paths()[1]


def usage_log_path():
    """wb_proxy 把它放在同一目录。"""
    return _data_paths()[2]


def overrides_path():
    """面板手填的映射文件，与源码里的 OVERRIDES 分开：运行期覆盖落在 usage/ 数据目录里，升级镜像
    不会丢，也不会被镜像覆盖。"""
    return _data_paths()[3]


_runtime_overrides_cache = {"key": None, "data": {}}


def _read_runtime_overrides():
    """只有文件缺失或损坏时忽略。"""
    data = {}
    try:
        with open(overrides_path(), encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            for mid, ref in loaded.items():
                mid, ref = str(mid or "").strip(), str(ref or "").strip()
                if mid and ref:
                    data[mid] = ref
    except Exception:
        data = {}
    return data


def runtime_overrides():
    """{hub id: OpenRouter id}——面板手填、按 mtime 缓存的覆盖映射。"""
    path = overrides_path()
    try:
        key = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return {}
    with _policy_lock:
        if _runtime_overrides_cache["key"] == key:
            return _runtime_overrides_cache["data"]
    data = _read_runtime_overrides()
    with _policy_lock:
        _runtime_overrides_cache.update({"key": key, "data": data})
    return data


def save_runtime_override(model, or_id):
    """or_id 为空表示删除这条映射（回到源码覆盖表与自动匹配）；返回写后的整份映射。"""
    model = str(model or "").strip()
    or_id = str(or_id or "").strip()
    path = overrides_path()
    with _policy_lock:
        data = _read_runtime_overrides()
        if or_id:
            data[model] = or_id
        else:
            data.pop(model, None)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
        _runtime_overrides_cache.update({"key": None, "data": data})
    return data


# 每行问一次「文件变了吗」就是一次 stat，路由器上是这趟扫描最大的开销。答案按路径缓存很短的 TTL：
# 外部写入最迟 _FILE_KEY_TTL 秒内可见（stat 从每行数次降到每秒一次），本进程写入立刻作废缓存。
_FILE_KEY_TTL = 1.0
_file_key_cache = {}
_file_key_lock = threading.Lock()


def _forget_file_key(path):
    """写入方在写完之后立刻调用：策略表在取价后的下一行就要能被读到，不能等满一个 TTL。"""
    with _file_key_lock:
        _file_key_cache.pop(path, None)


def _file_key(path):
    """(path, mtime, size)，按路径缓存 TTL；取不到时 None。"""
    now = time.monotonic()
    with _file_key_lock:
        hit = _file_key_cache.get(path)
        if hit is not None and hit[0] > now:
            return hit[1]
    try:
        # 一次 stat 同时取 mtime 和 size，比 getmtime + getsize 少一次系统调用。
        info = os.stat(path)
        key = (path, info.st_mtime, info.st_size)
    except OSError:
        key = None
    with _file_key_lock:
        if len(_file_key_cache) > 32:
            # 只为「路径会换」的场合兜底（换过数据目录、测试每用例一个临时目录）；清掉只会让下一次
            # 多 stat 一次，正确性不受驱逐策略影响。
            _file_key_cache.clear()
        _file_key_cache[path] = (now + _FILE_KEY_TTL, key)
    return key


def _read_jsonl(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except Exception:
        return []
    return rows


def policy_id(model, flat, bands, usd_cny):
    """内容哈希：同一模型同一计价方式得到同一 id。汇率参与哈希（它和美元价一样决定人民币数字），
    汇率变了就是另一条策略。"""
    payload = json.dumps(
        {"model": model, "flat": flat, "bands": bands or None,
         "usd_cny": round(_as_float(usd_cny, 0.0), 6)},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def load_policies():
    """{id: policy}：按内容去重的策略表。"""
    path = policies_path()
    key = _file_key(path)
    if key is None:
        return {}
    with _policy_lock:
        if _policies_cache["key"] == key and _policies_cache["data"] is not None:
            return _policies_cache["data"]
    data = {}
    for row in _read_jsonl(path):
        pid = row.get("id")
        if pid:
            data[pid] = row
    with _policy_lock:
        _policies_cache.update({"key": key, "data": data})
    return data


def load_timeline():
    """[{"at": epoch, "policies": {model: id}}]，最早在前。"""
    path = timeline_path()
    key = _file_key(path)
    if key is None:
        return []
    with _policy_lock:
        if _timeline_cache["key"] == key and _timeline_cache["data"] is not None:
            return _timeline_cache["data"]
    rows = [r for r in _read_jsonl(path) if isinstance(r.get("policies"), dict)]
    rows.sort(key=lambda r: _as_float(r.get("at")))
    with _policy_lock:
        _timeline_cache.update({"key": key, "data": rows})
    return rows


def current_assignment():
    """(epoch, {model: policy id})：时间线最后一行；尚未抓取过时为 (None, {})。"""
    timeline = load_timeline()
    if not timeline:
        return None, {}
    last = timeline[-1]
    return _as_float(last.get("at")), dict(last.get("policies") or {})


def current_policy_id(model):
    """这个模型当前生效的策略 id，供 wb_proxy 盖在行上。还没有策略的模型按上次抓取留在内存的
    OpenRouter 索引即时定价（绝不联网，请求路径不能被抓取阻塞），两次刷新之间首次调用的模型也有价。
    总开关关闭时一概不定价：不盖引用，也不生成按需策略。"""
    if not pricing_enabled():
        return None
    _at, assignment = current_assignment()
    pid = assignment.get(model)
    if pid:
        return pid
    return ensure_policy(model)


# ---- 按需计价 ------------------------------------------------------------
# 模型出现在最近一次成功的读数里但还没有策略（上游刚加、请求先于下一轮刷新）时，记账当场按该
# 索引计价，第一行就带上价格。

_live_lock = threading.Lock()
_live_index = {"models": None, "by_norm": None, "at": None}


def remember_live_index(or_models):
    """只存内存；抓取失败不调用，上一个好读数一直可用。"""
    if not or_models:
        return
    by_norm = index_openrouter(or_models)
    with _live_lock:
        _live_index.update({"models": dict(or_models), "by_norm": by_norm,
                            "at": time.time()})


def live_index():
    """最近一次成功抓取的 (or_models, by_norm)；没有时为 (None, {})。"""
    with _live_lock:
        return _live_index["models"], (_live_index["by_norm"] or {})


# ---- 网关互操作 -------------------------------------------------------------
# 网关通常作为库导入，容器里则是 `python wb_proxy.py` 启动（实例叫 __main__）。直接 import
# wb_proxy 会在同一进程里再执行一遍文件，那份副本没有账号池、目录为空、日志进不了面板；先找
# 正在运行的实例，都没加载时才补一次导入，查找规则统一在 wb_host。

def gateway_module():
    """正在运行的网关模块；都没加载时按 wb_proxy 导入一份。"""
    module = wb_host.loaded_gateway("add_log_entry")
    if module is not None:
        return module
    module = sys.modules.get("wb_proxy")
    if module is not None:
        return module
    import wb_proxy as module
    return module


def _log_pricing(msg):
    """只镜像进网关日志；定价不依赖它。"""
    try:
        gateway_module().add_log_entry("[定价] %s" % msg, tag="pricing")
    except Exception:
        pass


def ensure_policy(model):
    """从内存索引为没有策略的模型登记一条，返回策略 id。索引是冷的、名字解析不到或出了任何差错都
    返回 None，该行当作未定价；全程不联网。策略按内容哈希命名，重复调用是幂等的，只有真正新增的
    策略才记一行日志。总开关关闭时同样短路：不生成策略，也不写策略表。"""
    if not pricing_enabled():
        return None
    mid = str(model or "").strip()
    if not mid:
        return None
    or_models, by_norm = live_index()
    if not or_models:
        return None
    try:
        inherited = inherited_from(mid, or_models, by_norm)
        ref, via = resolve(mid, or_models, by_norm)
        if not ref:
            return None
        entry = entry_for(mid, ref, or_models, inherited=inherited,
                          overridden=via)
        doc = {"meta": {"base_currency": "CNY", "usd_cny": USD_CNY},
               "models": {mid: entry}}
        added, _changed, assignment = record_policies(doc, merge=True)
        pid = assignment.get(mid)
        if added:
            _log_pricing("按需补价：%s → %s%s（策略 %s）"
                         % (mid, ref,
                            "（继承自 %s）" % inherited if inherited
                            else ("（人工映射）" if via else ""), pid))
        return pid
    except Exception as exc:
        _log_pricing("按需补价失败（按未定价处理）：%s（%s）" % (mid, exc))
        return None


# ---- 未定价可见性 --------------------------------------------------------
# 候选仅是建议，这里不改任何价格。

_ALIAS_FALLBACK = ("default-model", "fast-model", "balanced-model",
                   "primary-model", "deep-model", "auto")


def virtual_alias_names():
    """虚拟档位别名集合（不是可选模型）；目录模块不可用时用本地兜底。"""
    try:
        import wb_models
        names = getattr(wb_models, "VIRTUAL_ALIAS_MODELS", None)
        if names:
            return {str(n) for n in names}
    except Exception:
        pass
    return set(_ALIAS_FALLBACK)


def suggest_matches(hub_id, or_models, limit=3):
    """最像 `hub_id` 的 OpenRouter id，仅作建议，不直接生效。先在归一化名字上做子串包含（便宜，渠道
    后缀通常就这样显形），其余的再看编辑距离比率。"""
    target = normalize(hub_id)
    if len(target) < 3 or not or_models:
        return []
    scored = []
    for mid in or_models:
        if ":" in mid:
            continue
        leaf = normalize(mid.split("/")[-1])
        if not leaf:
            continue
        if leaf in target or target in leaf:
            score = 0.75 + 0.25 * min(len(leaf), len(target)) / max(len(leaf), len(target))
        else:
            score = _ratio(target, leaf)
        if score >= 0.6:
            scored.append((score, mid))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [mid for _score, mid in scored[:limit]]


def _ratio(a, b):
    if not a or not b:
        return 0.0
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.6:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


def gap_items(or_models, by_norm=None, extra_ids=None, variants=None, priced=()):
    """(items, summary)：未定价模型，按原因分类，并附候选建议。实时价、出厂快照或内存索引能定价的
    都算已定价（按需路径会在首次调用时补上），剩下的才是长期缺口，按三种原因上报：
      - alias：虚拟条目（default-model 之类的别名），不是模型，不计入面板的缺口数，也不带候选；
      - variant_unmatched：名字带变体后缀，剥掉后缀的基名也匹配不上或不唯一；
      - or_missing：OpenRouter 没有对应条目，运维可在面板手工映射，候选即为此准备。
    """
    if variants is None:
        variants = variant_inherit_enabled()
    by_norm = by_norm if by_norm is not None else index_openrouter(or_models)
    aliases = virtual_alias_names()
    known = set(priced or ()) | set((load_pricing().get("models") or {}))
    items, counts = [], {"alias": 0, "or_missing": 0, "variant_unmatched": 0}
    for mid in priced_model_ids(extra_ids):
        if mid in known:
            continue
        if or_models and resolve(mid, or_models, by_norm, variants=variants)[0]:
            continue
        if mid in aliases:
            reason = "alias"
        elif any(True for _ in variant_bases(mid)):
            reason = "variant_unmatched"
        else:
            reason = "or_missing"
        counts[reason] += 1
        item = {"model": mid, "reason": reason}
        if reason != "alias":
            item["candidates"] = suggest_matches(mid, or_models)
        items.append(item)
    summary = {
        "total": counts["or_missing"] + counts["variant_unmatched"],
        "or_missing": counts["or_missing"],
        "variant_unmatched": counts["variant_unmatched"],
        "aliases": counts["alias"],
        "variants_enabled": bool(variants),
    }
    return items, summary


# 按策略对象身份缓存（entry 只由策略行内容决定，刷新后 load_policies 交回新行对象，键自然不同）；
# 缓存握住策略对象防身份复用，entry 只被 compute_row 与 _details_for 读、无人修改（rates 另拷）。
_entry_memo = {}


def policy_entry(policy):
    key = id(policy)
    hit = _entry_memo.get(key)
    if hit is not None and hit[0] is policy:
        return hit[1]
    entry = {
        "display": policy.get("model"),
        "source": "openrouter",
        "currency": policy.get("currency") or "USD",
        "unit": policy.get("unit") or 1000000,
        "or_id": policy.get("or_id"),
        "flat": policy.get("flat") or {},
        # 展示字段，与 entry_for 写进快照的同一套；老策略行没有这几个键，
        # cost_for_row 会用当前映射表去解释。
        "via": policy.get("via"),
        "inherited_from": policy.get("inherited_from"),
        "override_from": policy.get("override_from"),
    }
    if policy.get("bands"):
        entry["bands"] = policy["bands"]
    _memo_put(_entry_memo, key, (policy, entry))
    return entry


def _doc_for(policy, model):
    return {
        "meta": {"base_currency": "CNY",
                 "usd_cny": _as_float(policy.get("usd_cny"), USD_CNY)},
        "models": {model: policy_entry(policy)},
    }


def record_policies(doc, at=None, merge=False):
    """登记一批抓到的快照：先写策略表，再写时间线。返回 (新策略, 是否追加了时间线, {model: id})。
    表里已有的价不再重复写，时间线只在分配真的变化时才追加，价格稳定时无论刷新多密都不产生写入。
    merge=True 时以当前生效的分配为起点叠加本文档：一次按需登记（两个抓取周期之间的单个模型）不能
    让面板的「当前生效」视图丢掉其它模型。读取、判断、追加整段在一把可重入锁里完成：两个线程同时给
    同一个新模型定价时，策略行只追加一次，时间线行也只写一次。"""
    stamp = time.time() if at is None else _as_float(at)
    models = doc.get("models") or {}
    usd = _as_float((doc.get("meta") or {}).get("usd_cny"), USD_CNY)
    with _policy_lock:
        known = load_policies()
        fresh, assignment = [], {}
        _previous_at, previous = current_assignment()
        if merge:
            assignment = dict(previous)
        for model, entry in models.items():
            pid = policy_id(model, entry.get("flat"), entry.get("bands"), usd)
            assignment[model] = pid
            if pid in known:
                continue
            known[pid] = {
                "id": pid,
                "model": model,
                "or_id": entry.get("or_id"),
                "currency": entry.get("currency") or "USD",
                "unit": entry.get("unit") or 1000000,
                "usd_cny": usd,
                "flat": entry.get("flat") or {},
                "bands": entry.get("bands"),
                # 审计痕迹：直接匹配、人工映射还是基准模型继承（via="variant" 时 inherited_from
                # 给基准名，via="override" 时 override_from 给原始 hub 模型名）。不进哈希。
                "via": entry.get("via") or "direct",
                "inherited_from": entry.get("inherited_from"),
                "override_from": entry.get("override_from"),
                "first_seen": stamp,
            }
            fresh.append(known[pid])
        if fresh:
            path = policies_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for row in fresh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            _policies_cache.update({"key": None, "data": None})
            _forget_file_key(path)
        changed = False
        if assignment and assignment != previous:
            path = timeline_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": stamp, "policies": assignment},
                                    ensure_ascii=False) + "\n")
            _timeline_cache.update({"key": None, "data": None})
            _forget_file_key(path)
            changed = True
    return len(fresh), changed, assignment


def referenced_policy_ids():
    ids = set()
    try:
        with open(usage_log_path(), encoding="utf-8") as fh:
            for line in fh:
                if '"cost_policy"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                pid = row.get("cost_policy")
                if pid:
                    ids.add(pid)
    except Exception:
        return set()
    return ids


def prune_policies(protect=()):
    """删掉没有任何引用的策略，返回被删除的 id。保留：模型的当前生效项、用量行引用的每个 id、
    调用方固定的条目（本轮刚抓的），以及会因此失去最后一条策略的模型的最新策略（保证任何模型都有
    价可依）。"""
    policies = load_policies()
    if not policies:
        return []
    _at, assignment = current_assignment()
    keep = set(assignment.values()) | set(referenced_policy_ids()) | set(protect)
    by_model = {}
    for pid, policy in policies.items():
        by_model.setdefault(policy.get("model"), []).append(pid)
    for ids in by_model.values():
        if any(pid in keep for pid in ids):
            continue
        newest = max(ids, key=lambda p: _as_float((policies[p] or {}).get("first_seen")))
        keep.add(newest)
    removed = [pid for pid in policies if pid not in keep]
    if not removed:
        return []
    path = policies_path()
    tmp = path + ".tmp"
    with _policy_lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            for pid, policy in policies.items():
                if pid in keep:
                    fh.write(json.dumps(policy, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        _policies_cache.update({"key": None, "data": None})
        _forget_file_key(path)
    return removed


# load_timeline() 对同一份文件内容交回同一列表对象，「是不是同一份时间线」按对象身份判断即可；
# 时刻列随列表缓存（policy_ref_at 每行二分一次，不必每行重取 at 转 float），改文件自然重算。
_timeline_ats_cache = {"rows": None, "ats": None}


def _timeline_ats(timeline):
    """按 timeline 对象缓存一次。"""
    cached = _timeline_ats_cache
    if cached["rows"] is timeline:
        return cached["ats"]
    ats = [_as_float(r.get("at")) for r in timeline]
    with _policy_lock:
        _timeline_ats_cache.update({"rows": timeline, "ats": ats})
    return ats


def policy_ref_at(epoch_sec, model):
    """（策略 id, backfilled）：过去的某一刻该用哪份策略。

    - 时刻之前最后一条列出该模型的时间线行；
    - 否则取之后第一条列出它的行（价格是事后才拿到的），标为 backfilled；
    - 再不然取更早的一行，同样标为 backfilled（对应已被下架的模型）；
    - 时间线为空时返回 (None, False)，由调用方回落到出厂快照。
    """
    timeline = load_timeline()
    if not timeline:
        return None, False
    stamp = _as_float(epoch_sec)
    index = bisect.bisect_right(_timeline_ats(timeline), stamp) - 1
    if index < 0:
        index = 0
    rows = timeline[index].get("policies") or {}
    if model in rows:
        return rows[model], False
    for row in timeline:
        if _as_float(row.get("at")) < stamp:
            continue
        hit = (row.get("policies") or {}).get(model)
        if hit:
            return hit, True
    for row in reversed(timeline[:index + 1]):
        hit = (row.get("policies") or {}).get(model)
        if hit:
            return hit, True
    return None, False


# ---- 一行价格的构成 ------------------------------------------------------
# 把 cost_for_row 已经决定好的东西摊开，不改任何计价口径。

_WEEKDAY_CN = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
               "fri": "周五", "sat": "周六", "sun": "周日"}

# 明细字段的固定顺序，未定价时整组给 None，不编 0。
_DETAIL_KEYS = ("rates", "unit", "currency", "usd_cny", "or_id", "via",
                "inherited_from", "override_from", "band_note", "via_derived")


def _hhmm(value):
    """1600 -> 16:00；2400 是源里「当天结束」的写法。"""
    minutes = int(_as_float(value))
    return "%02d:%02d" % (minutes // 100, minutes % 100)


def band_note(entry, index):
    """这一行凭什么落在这个条件档位；没有档位（或索引越界）时 None。"""
    bands = entry.get("bands")
    if index is None or not isinstance(bands, list):
        return None
    if not isinstance(index, int) or not 0 <= index < len(bands):
        return None
    band = bands[index]
    if not isinstance(band, dict):
        return None
    threshold = band.get("min_prompt_tokens")
    if threshold is not None:
        return "输入长度 ≥ %s tokens" % format(int(_as_float(threshold)), ",")
    days = [day for day in (band.get("days") or []) if day in _WEEKDAY_CN]
    window = "%s–%s UTC" % (_hhmm(band.get("start")), _hhmm(band.get("end")))
    if days:
        return "每周 %s %s" % ("/".join(_WEEKDAY_CN[day] for day in days), window)
    return "每天 %s" % window


def match_source(entry, model):
    """(via, inherited_from, override_from, derived)。策略行里记着 via 就直接用；更早写下的策略行
    没有这个字段（字段是后加的），只能拿当前映射表解释：表里指的正是这条 or_id 就按 override 记并
    标为推断；表改过或指不到这条就按直接匹配记。"""
    via = str(entry.get("via") or "").strip()
    if via in ("variant", "override", "direct"):
        return via, entry.get("inherited_from"), entry.get("override_from"), False
    mapping = runtime_overrides().get(model) or OVERRIDES.get(model)
    if mapping and mapping == entry.get("or_id"):
        return "override", None, model, True
    return "direct", None, None, False


def _no_details():
    return {key: None for key in _DETAIL_KEYS}


# 明细模板按 (entry, 档, meta, 行名) 的身份缓存，命中只把 rates 拷一份（与逐字段重建逐键一致，
# 含键序）；只有记着 via 的行进缓存（老行推断随覆盖表变化），值握住 entry/meta/rates 防身份复用。
_details_memo = {}


def _details_for(cost, entry, meta, model):
    if not cost.get("known") or not isinstance(entry, dict):
        return _no_details()
    rates = cost.get("rates")
    recorded = entry.get("via")
    key = None
    if recorded in ("variant", "override", "direct"):
        key = (id(entry), id(meta), id(rates), cost.get("band"), model)
        hit = _details_memo.get(key)
        if hit is not None:
            # hit 是（entry, meta, rates, 模板）：模板照抄一份，rates 按源表另拷。
            out = dict(hit[3])
            out["rates"] = dict(hit[2] or {})
            return out
    via, inherited, override_from, derived = match_source(entry, model)
    template = {
        # 源表：交给调用方的那一份在下面另拷，缓存里留的是原对象。
        "rates": rates or {},
        "unit": _as_float(entry.get("unit")) or 1000000.0,
        "currency": entry.get("currency") or "USD",
        "usd_cny": _as_float((meta or {}).get("usd_cny")),
        "or_id": entry.get("or_id"),
        "via": via,
        "inherited_from": inherited,
        "override_from": override_from,
        "band_note": band_note(entry, cost.get("band")),
        "via_derived": derived,
    }
    if key is not None:
        _memo_put(_details_memo, key, (entry, meta, rates, template))
    out = dict(template)
    out["rates"] = dict(rates or {})
    return out


def cost_for_row(row, details=True, enabled=None, pricing_doc=None):
    """一行 usage → {"cny", "known", "band", "source", "source_at", "backfilled"}，外加悬停要用的明细
    （rates / unit / currency / usd_cny / or_id / via / 来源与档位说明）。定价来源：行上记录的策略引用
    优先，没有引用的老行回落到时间线，完全没有策略的模型按出厂快照定价；这里是决定一次请求按哪份价
    计量的唯一位置，逐行视图与各汇总因此天然一致。明细只解释这一行用的是哪份价，未定价时整组 None
    （不编 0）。总开关关闭时整块停摆：不查策略、不查快照、不折算，按未定价返回并打上 disabled 标记
    （面板显示「—」），汇总侧仍能区分真正无价与停摆。details=False 供聚合扫描（每行只读 known / cny
    与 disabled 标记）；enabled / pricing_doc 是开关与价目表在循环外各取一次（None 表示逐行自查 /
    现取），聚合都只看扫描开始那一刻的值，改动自下一次扫描生效。"""
    if not row or not (pricing_enabled() if enabled is None else enabled):
        cost = {"cny": 0.0, "known": False, "band": None, "source": "builtin",
                "source_at": None, "backfilled": False}
        if row:
            cost["disabled"] = True
        if details:
            cost.update(_no_details())
        return cost
    model = row.get("model")
    pid = row.get("cost_policy")
    backfilled = False
    if not pid:
        pid, backfilled = policy_ref_at(row.get("at"), model)
    policy = load_policies().get(pid) if pid else None
    if policy:
        cost = compute_row(row, pricing=_doc_for(policy, model))
        cost["source"] = pid
        cost["source_at"] = _as_float(policy.get("first_seen"))
        cost["backfilled"] = backfilled
        if details:
            cost.update(_details_for(cost, policy_entry(policy), policy, model))
        return cost
    doc = pricing_doc if pricing_doc is not None else load_pricing()
    entry = (doc.get("models") or {}).get(model)
    cost = compute_row(row, pricing=doc)
    cost["source"] = "builtin"
    cost["source_at"] = None
    cost["backfilled"] = False
    if details:
        cost.update(_details_for(cost, entry, doc.get("meta"), model))
    return cost


def policy_label(policy):
    """策略的挂钟标签（价格首次出现的时刻）。"""
    stamp = _as_float((policy or {}).get("first_seen"))
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp)) if stamp else None


# ---- 后台刷新 ------------------------------------------------------------
# 间隔单位分钟（0 表示关闭）。抓取失败不改动任何东西，上一个好版本继续计价；价格真的变了才动
# 策略，短间隔每轮只花一次 HTTP GET。

DEFAULT_REFRESH_MINUTES = 5


class PriceRefresher(threading.Thread):

    def __init__(self, interval_minutes=DEFAULT_REFRESH_MINUTES):
        super().__init__(daemon=True, name="price-refresher")
        self.interval_minutes = max(0.0, _as_float(interval_minutes))
        self.last_run = None
        self.last_error = None
        self.last_models = 0
        self.last_extra = 0
        self._extra_ids = []
        self.last_unpriced = []
        self.next_run = None
        self.logs = []
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._run_lock = threading.Lock()
        self._gap_lock = threading.Lock()
        self._gap_cache = None

    def log(self, msg):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append("[%s] %s" % (stamp, msg))
        if len(self.logs) > 40:
            self.logs = self.logs[-40:]
        try:
            gateway_module().add_log_entry("[定价] %s" % msg, tag="pricing")
        except Exception:
            pass

    def interval_seconds(self):
        """单位换算只在这一处。"""
        return max(0.0, _as_float(self.interval_minutes)) * 60.0

    def set_interval(self, minutes):
        """正在等待的循环立即采用。"""
        self.interval_minutes = max(0.0, _as_float(minutes))
        self._wake.set()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def live_ids(self):
        """网关实时目录里的模型 id，过滤器与 /v1/models 一致（选择器能列出、内置快照却没有的模型）。
        走运行中的网关模块，共用 is_chat_model 与 curate_remote_catalog 的变体/免费兄弟规则，不复写
        一份；两个区域都要问（实时目录按区域抓取，只认 intl/cn）。任何失败都退化为空列表——静态目录
        照常定价，实时目录出问题只会留下缺口，不会缩小覆盖。"""
        try:
            import wb_models
            known = set(hub_model_ids())
            out, seen = [], set()
            for realm in LIVE_CATALOG_REALMS:
                try:
                    # （entries, extras, source）：定价只读 entries。
                    entries, _extras, _source = wb_models.curated_live_sources(realm)
                except Exception as exc:
                    self.log("live 目录取不到（%s），该区域本轮只按内置目录：%s"
                             % (realm, exc))
                    continue
                for mid, _meta in entries or []:
                    mid = str(mid or "").strip()
                    if mid and mid not in known and mid not in seen:
                        seen.add(mid)
                        out.append(mid)
            return out
        except Exception as exc:
            self.log("live 目录取不到，本轮只覆盖内置目录：%s" % exc)
            return []

    def run_once(self):
        """返回 (ok, message)。"""
        with self._run_lock:
            # 在锁内重读总开关：面板刚关定价时已启动的这轮刷新，不得在收尾时
            # 写出一份新的策略表。
            if not pricing_enabled():
                return False, "价估算已关闭"
            try:
                or_models = fetch_openrouter()
            except Exception as exc:
                self.last_error = str(exc)
                self.log("抓取失败，继续用上一版价格：%s" % exc)
                return False, str(exc)
            extra_ids = self.live_ids()
            previous = (load_pricing().get("models") or {})
            doc, unpriced, overridden = build_snapshot(or_models, previous, extra_ids)
            if not doc["models"]:
                self.last_error = "抓到了模型列表，但一个都没匹配上"
                self.log(self.last_error)
                return False, self.last_error
            stamp = time.time()
            added, _changed, assignment = record_policies(doc, at=stamp)
            # 按需定价路径读这份索引；只有抓取真正成功才发布，失败的轮次保留
            # 上一个好索引。
            remember_live_index(or_models)
            self.last_run = stamp
            self.last_error = None
            self.last_models = len(doc["models"])
            self.last_extra = len(extra_ids)
            # 缺口报告：只在在线目录里、无法定价的名字也要进来。只在内存里，该路径不联网。
            self._extra_ids = list(extra_ids)
            self.last_unpriced = list(unpriced)
            self.invalidate_gaps()
            removed = []
            if added:
                # 只有从未见过的价格才可能让旧价格失去引用，所以清扫只在有
                # 新价格时跑，不逐轮执行。
                removed = prune_policies(protect=set(assignment.values()))
            self.log("已取价：%d 个模型（新增策略 %d，人工覆盖 %d，未定价 %d%s%s）"
                     % (self.last_models, added, len(overridden), len(unpriced),
                        "，live 新增 %d" % self.last_extra if self.last_extra else "",
                        "，清理无用策略 %d" % len(removed) if removed else ""))
            return True, ""

    # ---- 未定价可见性（面板） --------------------------------------------
    def invalidate_gaps(self):
        with self._gap_lock:
            self._gap_cache = None

    def gap_report(self, max_items=200, ttl=30.0):
        """(items, summary)：哪些模型显示「—」，以及原因，供面板使用。数据来自内存索引与已存策略表，
        外加上一轮成功抓取的实时 id（上游刚加、OpenRouter 还没有的模型必须在这里露出来）。设置页会
        轮询它，因此缓存一小段时间。"""
        with self._gap_lock:
            cached = self._gap_cache
            if cached and time.time() - cached["at"] < ttl:
                return cached["items"], cached["summary"]
        or_models, by_norm = live_index()
        _at, assignment = current_assignment()
        items, summary = gap_items(or_models, by_norm, extra_ids=self._extra_ids,
                                   variants=variant_inherit_enabled(),
                                   priced=set(assignment))
        if max_items and len(items) > max_items:
            items = items[:max_items]
        with self._gap_lock:
            self._gap_cache = {"at": time.time(), "items": items, "summary": summary}
        return items, summary

    def run(self):
        # 两种情况要立刻抓一次价：首次启动还没有任何价格历史，以及总开关刚从关闭切回打开。后者
        # 是补算的关键：关闭期间的请求由 policy_ref_at 补算分支取到这份价，新模型借此进入策略表。
        first = True
        came_from_off = False
        while not self._stop_event.is_set():
            if not pricing_enabled():
                # 总开关关着：不取价也不计时。面板拨回打开时会唤醒本线程，
                # 循环每轮都重读开关，所以打开后立刻恢复。
                self.next_run = None
                self._wake.wait()
                self._wake.clear()
                came_from_off = True
                continue
            seconds = self.interval_seconds()
            if seconds <= 0:
                self.next_run = None
                self._wake.wait()
                self._wake.clear()
                came_from_off = True
                continue
            if first or came_from_off:
                should_fetch = came_from_off or not load_timeline()
                first = False
                came_from_off = False
                if should_fetch:
                    try:
                        self.run_once()
                    except Exception as exc:
                        self.log("首次抓取异常：%s" % exc)
            self.next_run = time.time() + seconds
            if self._wake.wait(seconds):
                self._wake.clear()
                continue
            if self._stop_event.is_set():
                break
            try:
                self.run_once()
            except Exception as exc:
                self.last_error = str(exc)
                self.log("刷新异常：%s" % exc)

    def status(self):
        policies = load_policies()
        at, assignment = current_assignment()
        gaps, gap_summary = self.gap_report()
        master = pricing_enabled()
        return {
            "interval_minutes": self.interval_minutes,
            # 总开关关着时，无论间隔填多少都不取价，所以 enabled 是两者相与；
            # master_enabled 单独回报开关本身，面板据此渲染设置页。
            "enabled": master and self.interval_minutes > 0,
            "master_enabled": master,
            "running": self.is_alive(),
            "last_run": self.last_run,
            "last_run_label": (time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(self.last_run))
                               if self.last_run else None),
            "next_run": self.next_run,
            "next_run_label": (time.strftime("%Y-%m-%d %H:%M",
                                             time.localtime(self.next_run))
                               if self.next_run else None),
            "last_error": self.last_error,
            "last_models": self.last_models,
            "last_extra": self.last_extra,
            "policies": len(policies),
            "models": len(assignment),
            "current_at": at,
            "current_at_label": (time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(at)) if at else None),
            # 逐模型的当前生效项；变体同样给出，并注明价格继承自哪个模型。
            "current": {m: {"id": pid,
                            "label": policy_label(policies.get(pid) or {}),
                            "via": (policies.get(pid) or {}).get("via") or "direct",
                            "inherited_from": (policies.get(pid) or {}).get("inherited_from")}
                        for m, pid in sorted(assignment.items())},
            "gaps": gaps,
            "gap_summary": gap_summary,
            "variant_inherit": variant_inherit_enabled(),
            "overrides_file": overrides_path(),
            "policies_file": policies_path(),
            "timeline": timeline_path(),
            "logs": list(self.logs),
        }
