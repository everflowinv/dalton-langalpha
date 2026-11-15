#!/usr/bin/env python3
"""
screen_movers.py — weekly price/volume screen for Richard's coverage list.

For every covered name: window return vs peer-group median, daily excess return vs peers,
z-score of daily excess (60-day history), and volume ratio vs the prior 20-day average.
Core names (deep tier) are always reported in full; Watch names are flagged when they move
unusually versus peers.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
import warnings
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")


def _load_richard_cov_io() -> Any:
    cached = sys.modules.get("richard_cov_io")
    if cached is not None:
        return cached
    target = Path(__file__).resolve().with_name("richard_cov_io.py")
    spec = importlib.util.spec_from_file_location("richard_cov_io", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["richard_cov_io"] = mod
    spec.loader.exec_module(mod)
    return mod


_io = _load_richard_cov_io()
workspace_file = _io.workspace_file
resolve_workspace_target = _io.resolve_workspace_target
write_target_text = _io.write_target_text

# name -> (ticker, peer group, sheet section, tier). Names follow the Weekly Risk Reward (Richard)
# sheet of 2026-10-09. Tier "Core" = deep tier (searched one by one); "Watch" = screened by
# relative performance first; "" = not covered, used only as a peer in its group.
# Tickers checked against the sheet's 2026-10-08 closes. Renames: EchoStar ECHO (ex-SATS),
# Marsh MRSH, Fiserv FISV, Block XYZ; A-shares use Yahoo's .SS / .SZ suffix.
TICKERS: dict[str, tuple[str, str, str, str]] = {
    # Live Position
    "Google": ("GOOGL", "megacap", "Live Position", "Core"),
    "Amazon": ("AMZN", "megacap", "Live Position", "Core"),
    "DoorDash": ("DASH", "delivery", "Live Position", "Core"),
    "Affirm": ("AFRM", "bnpl", "Live Position", "Core"),
    "EchoStar": ("ECHO", "space", "Live Position", "Watch"),
    "D.R. Horton": ("DHI", "homebuilder", "Live Position", "Watch"),
    "唯品会": ("VIPS", "cn_ecom_vertical", "Live Position", "Watch"),
    "亚朵": ("ATAT", "cn_travel_local", "Live Position", "Core"),
    "Boss直聘": ("BZ", "cn_internet_small", "Live Position", "Core"),
    # US Mega Tech
    "Apple": ("AAPL", "megacap", "US Mega Tech", "Watch"),
    "Microsoft": ("MSFT", "megacap", "US Mega Tech", "Core"),
    "SpaceX": ("SPCX", "space", "US Mega Tech", "Core"),
    "Meta": ("META", "megacap", "US Mega Tech", "Core"),
    "Tesla": ("TSLA", "megacap", "US Mega Tech", "Watch"),
    # AI Native
    "智谱": ("2513.HK", "cn_ai", "AI Native", "Core"),
    "Minimax": ("0100.HK", "cn_ai", "AI Native", "Watch"),
    # US Internet
    "Netflix": ("NFLX", "streaming_media", "US Internet", "Watch"),
    "Spotify": ("SPOT", "streaming_media", "US Internet", "Watch"),
    "MercadoLibre": ("MELI", "ecom", "US Internet", "Watch"),
    "Sea": ("SE", "ecom", "US Internet", "Watch"),
    "Coupang": ("CPNG", "ecom", "US Internet", "Watch"),
    "Grab": ("GRAB", "delivery", "US Internet", ""),
    "Uber": ("UBER", "delivery", "US Internet", "Core"),
    "Instacart": ("CART", "delivery", "US Internet", ""),
    "Lyft": ("LYFT", "delivery", "US Internet", ""),
    "Booking": ("BKNG", "online_travel", "US Internet", "Watch"),
    "Airbnb": ("ABNB", "online_travel", "US Internet", "Watch"),
    "Expedia": ("EXPE", "online_travel", "US Internet", "Watch"),
    "eBay": ("EBAY", "ecom", "US Internet", ""),
    "Wayfair": ("W", "ecom", "US Internet", ""),
    "Chewy": ("CHWY", "ecom", "US Internet", ""),
    "Etsy": ("ETSY", "ecom", "US Internet", ""),
    "Zillow": ("ZG", "property_info", "US Internet", ""),
    "Take-Two": ("TTWO", "games_adtech", "US Internet", ""),
    "Roblox": ("RBLX", "games_adtech", "US Internet", "Watch"),
    "Duolingo": ("DUOL", "social_apps", "US Internet", "Watch"),
    "AppLovin": ("APP", "games_adtech", "US Internet", "Watch"),
    "Unity": ("U", "games_adtech", "US Internet", "Watch"),
    "Reddit": ("RDDT", "social_apps", "US Internet", "Watch"),
    "Pinterest": ("PINS", "social_apps", "US Internet", ""),
    "Snap": ("SNAP", "social_apps", "US Internet", ""),
    "Match": ("MTCH", "social_apps", "US Internet", "Watch"),
    "Disney": ("DIS", "streaming_media", "US Internet", ""),
    "Fox": ("FOXA", "streaming_media", "US Internet", ""),
    "Formula One": ("FWONK", "streaming_media", "US Internet", ""),
    "Roku": ("ROKU", "streaming_media", "US Internet", ""),
    "Warner Music": ("WMG", "streaming_media", "US Internet", ""),
    # US Financial
    "JPMorgan": ("JPM", "banks", "US Financial", "Watch"),
    "BofA": ("BAC", "banks", "US Financial", "Watch"),
    "Wells Fargo": ("WFC", "banks", "US Financial", ""),
    "Citi": ("C", "banks", "US Financial", ""),
    "Morgan Stanley": ("MS", "banks", "US Financial", ""),
    "Goldman Sachs": ("GS", "banks", "US Financial", ""),
    "BNY": ("BNY", "banks", "US Financial", ""),
    "State Street": ("STT", "banks", "US Financial", ""),
    "Visa": ("V", "networks", "US Financial", "Core"),
    "Mastercard": ("MA", "networks", "US Financial", "Core"),
    "Shopify": ("SHOP", "ecom", "US Financial", "Watch"),
    "PayPal": ("PYPL", "payments", "US Financial", "Watch"),
    "Block": ("XYZ", "payments", "US Financial", "Watch"),
    "Adyen": ("ADYEY", "payments", "US Financial", ""),
    "Corpay": ("CPAY", "payments", "US Financial", ""),
    "Fiserv": ("FISV", "payments", "US Financial", ""),
    "Global Payments": ("GPN", "payments", "US Financial", ""),
    "FIS": ("FIS", "payments", "US Financial", ""),
    "Toast": ("TOST", "payments", "US Financial", ""),
    "Chime": ("CHYM", "fintech_lenders", "US Financial", "Watch"),
    "Amex": ("AXP", "networks", "US Financial", ""),
    "Capital One": ("COF", "card_lenders", "US Financial", ""),
    "Synchrony": ("SYF", "card_lenders", "US Financial", ""),
    "SoFi": ("SOFI", "fintech_lenders", "US Financial", "Watch"),
    "Ally": ("ALLY", "card_lenders", "US Financial", ""),
    "Enova": ("ENVA", "fintech_lenders", "US Financial", ""),
    "Klarna": ("KLAR", "bnpl", "US Financial", "Watch"),
    "Sezzle": ("SEZL", "bnpl", "US Financial", "Watch"),
    "Upstart": ("UPST", "fintech_lenders", "US Financial", "Watch"),
    "Zip": ("ZIP.AX", "bnpl", "US Financial", ""),
    "Pagaya": ("PGY", "fintech_lenders", "US Financial", ""),
    "Robinhood": ("HOOD", "brokers", "US Financial", "Core"),
    "Coinbase": ("COIN", "crypto", "US Financial", ""),
    "Circle": ("CRCL", "crypto", "US Financial", ""),
    "Rocket": ("RKT", "fintech_lenders", "US Financial", "Watch"),
    "Figure": ("FIGR", "fintech_lenders", "US Financial", "Watch"),
    "Schwab": ("SCHW", "brokers", "US Financial", ""),
    "Raymond James": ("RJF", "brokers", "US Financial", ""),
    "LPL": ("LPLA", "brokers", "US Financial", ""),
    "Interactive Brokers": ("IBKR", "brokers", "US Financial", "Core"),
    "Virtu": ("VIRT", "brokers", "US Financial", ""),
    "StoneX": ("SNEX", "brokers", "US Financial", ""),
    "Blackstone": ("BX", "alts", "US Financial", "Watch"),
    "KKR": ("KKR", "alts", "US Financial", "Watch"),
    "Apollo": ("APO", "alts", "US Financial", "Watch"),
    "Ares": ("ARES", "alts", "US Financial", ""),
    "Blue Owl": ("OWL", "alts", "US Financial", ""),
    "BlackRock": ("BLK", "alts", "US Financial", ""),
    "T. Rowe Price": ("TROW", "alts", "US Financial", ""),
    "FICO": ("FICO", "bureaus", "US Financial", ""),
    "Experian": ("EXPN.L", "bureaus", "US Financial", ""),
    "Equifax": ("EFX", "bureaus", "US Financial", ""),
    "TransUnion": ("TRU", "bureaus", "US Financial", ""),
    "S&P Global": ("SPGI", "ratings_info", "US Financial", "Core"),
    "Moody's": ("MCO", "ratings_info", "US Financial", "Core"),
    "MSCI": ("MSCI", "ratings_info", "US Financial", ""),
    "Thomson Reuters": ("TRI", "ratings_info", "US Financial", ""),
    "Wolters Kluwer": ("WKL.AS", "ratings_info", "US Financial", ""),
    "RELX": ("REL.L", "ratings_info", "US Financial", ""),
    "Verisk": ("VRSK", "ratings_info", "US Financial", ""),
    "CoStar": ("CSGP", "property_info", "US Financial", ""),
    "Berkshire": ("BRK-B", "insurers", "US Financial", "Watch"),
    "Chubb": ("CB", "insurers", "US Financial", ""),
    "Progressive": ("PGR", "insurers", "US Financial", "Watch"),
    "Travelers": ("TRV", "insurers", "US Financial", ""),
    "Berkley": ("WRB", "insurers", "US Financial", ""),
    "Kinsale": ("KNSL", "insurers", "US Financial", ""),
    "Lemonade": ("LMND", "insurers", "US Financial", ""),
    "Root": ("ROOT", "insurers", "US Financial", ""),
    "Marsh": ("MRSH", "ins_brokers", "US Financial", "Watch"),
    "Aon": ("AON", "ins_brokers", "US Financial", "Watch"),
    "Gallagher": ("AJG", "ins_brokers", "US Financial", "Watch"),
    "WTW": ("WTW", "ins_brokers", "US Financial", ""),
    "Brown & Brown": ("BRO", "ins_brokers", "US Financial", ""),
    "Ryan": ("RYAN", "ins_brokers", "US Financial", ""),
    "CME": ("CME", "exchanges", "US Financial", "Watch"),
    "ICE": ("ICE", "exchanges", "US Financial", "Watch"),
    "Cboe": ("CBOE", "exchanges", "US Financial", "Watch"),
    "Nasdaq": ("NDAQ", "exchanges", "US Financial", "Core"),
    "Nubank": ("NU", "fintech_lenders", "US Financial", ""),
    "AerCap": ("AER", "lessors", "US Financial", ""),
    "Wise": ("WISE.L", "payments", "US Financial", ""),
    # US Software
    "Palantir": ("PLTR", "infra_software", "US Software", "Watch"),
    "Salesforce": ("CRM", "app_software", "US Software", "Watch"),
    "ServiceNow": ("NOW", "app_software", "US Software", ""),
    "SAP": ("SAP", "app_software", "US Software", ""),
    "Adobe": ("ADBE", "app_software", "US Software", ""),
    "Intuit": ("INTU", "app_software", "US Software", ""),
    "Atlassian": ("TEAM", "app_software", "US Software", ""),
    "Autodesk": ("ADSK", "app_software", "US Software", ""),
    "Veeva": ("VEEV", "app_software", "US Software", ""),
    "Workday": ("WDAY", "app_software", "US Software", ""),
    "Roper": ("ROP", "app_software", "US Software", ""),
    "Zoom": ("ZM", "app_software", "US Software", ""),
    "Palo Alto Networks": ("PANW", "infra_software", "US Software", ""),
    "CrowdStrike": ("CRWD", "infra_software", "US Software", ""),
    "Fortinet": ("FTNT", "infra_software", "US Software", ""),
    "Cloudflare": ("NET", "infra_software", "US Software", "Watch"),
    "Okta": ("OKTA", "infra_software", "US Software", ""),
    "Zscaler": ("ZS", "infra_software", "US Software", ""),
    "Snowflake": ("SNOW", "infra_software", "US Software", ""),
    "Datadog": ("DDOG", "infra_software", "US Software", ""),
    "Twilio": ("TWLO", "infra_software", "US Software", ""),
    "MongoDB": ("MDB", "infra_software", "US Software", ""),
    "Rubrik": ("RBRK", "infra_software", "US Software", ""),
    "JFrog": ("FROG", "infra_software", "US Software", ""),
    "Verisign": ("VRSN", "infra_software", "US Software", ""),
    "GoDaddy": ("GDDY", "infra_software", "US Software", ""),
    "IBM": ("IBM", "it_services", "US Software", ""),
    "Accenture": ("ACN", "it_services", "US Software", ""),
    "Cognizant": ("CTSH", "it_services", "US Software", ""),
    "Capgemini": ("CAP.PA", "it_services", "US Software", ""),
    "Huron": ("HURN", "it_services", "US Software", "Watch"),
    # Global Consumer
    "Nike": ("NKE", "consumer_brands", "Global Consumer", ""),
    "Adidas": ("ADS.DE", "consumer_brands", "Global Consumer", ""),
    "ASICS": ("7936.T", "consumer_brands", "Global Consumer", ""),
    "Amer": ("AS", "consumer_brands", "Global Consumer", ""),
    "On": ("ONON", "consumer_brands", "Global Consumer", ""),
    "Deckers": ("DECK", "consumer_brands", "Global Consumer", ""),
    "Lululemon": ("LULU", "consumer_brands", "Global Consumer", ""),
    "LVMH": ("MC.PA", "consumer_brands", "Global Consumer", ""),
    "Hermès": ("RMS.PA", "consumer_brands", "Global Consumer", ""),
    "Richemont": ("CFR.SW", "consumer_brands", "Global Consumer", ""),
    "Dior": ("CDI.PA", "consumer_brands", "Global Consumer", ""),
    "Kering": ("KER.PA", "consumer_brands", "Global Consumer", ""),
    "Tapestry": ("TPR", "consumer_brands", "Global Consumer", ""),
    "Ralph Lauren": ("RL", "consumer_brands", "Global Consumer", ""),
    "SharkNinja": ("SN", "consumer_brands", "Global Consumer", ""),
    "Estée Lauder": ("EL", "staples", "Global Consumer", ""),
    "Coca-Cola": ("KO", "staples", "Global Consumer", ""),
    "PepsiCo": ("PEP", "staples", "Global Consumer", ""),
    "Monster": ("MNST", "staples", "Global Consumer", ""),
    "Constellation": ("STZ", "staples", "Global Consumer", ""),
    "Mondelez": ("MDLZ", "staples", "Global Consumer", ""),
    "Hershey": ("HSY", "staples", "Global Consumer", ""),
    "Kraft Heinz": ("KHC", "staples", "Global Consumer", ""),
    "P&G": ("PG", "staples", "Global Consumer", ""),
    "Colgate": ("CL", "staples", "Global Consumer", ""),
    "Philip Morris": ("PM", "staples", "Global Consumer", "Watch"),
    "Altria": ("MO", "staples", "Global Consumer", ""),
    "Walmart": ("WMT", "mass_retail", "Global Consumer", "Watch"),
    "Costco": ("COST", "mass_retail", "Global Consumer", ""),
    "Target": ("TGT", "mass_retail", "Global Consumer", ""),
    "Kroger": ("KR", "mass_retail", "Global Consumer", ""),
    "Dollar General": ("DG", "mass_retail", "Global Consumer", ""),
    "Home Depot": ("HD", "specialty_retail", "Global Consumer", ""),
    "Lowe's": ("LOW", "specialty_retail", "Global Consumer", ""),
    "TJX": ("TJX", "mass_retail", "Global Consumer", ""),
    "Ross": ("ROST", "mass_retail", "Global Consumer", ""),
    "Ulta": ("ULTA", "specialty_retail", "Global Consumer", ""),
    "O'Reilly": ("ORLY", "specialty_retail", "Global Consumer", ""),
    "Carvana": ("CVNA", "specialty_retail", "Global Consumer", ""),
    "AutoZone": ("AZO", "specialty_retail", "Global Consumer", ""),
    "Copart": ("CPRT", "specialty_retail", "Global Consumer", ""),
    "McDonald's": ("MCD", "restaurants", "Global Consumer", ""),
    "Starbucks": ("SBUX", "restaurants", "Global Consumer", ""),
    "Chipotle": ("CMG", "restaurants", "Global Consumer", ""),
    "Yum": ("YUM", "restaurants", "Global Consumer", ""),
    "Marriott": ("MAR", "lodging_cruise", "Global Consumer", "Watch"),
    "Hilton": ("HLT", "lodging_cruise", "Global Consumer", "Watch"),
    "Royal Caribbean": ("RCL", "lodging_cruise", "Global Consumer", ""),
    "Viking": ("VIK", "lodging_cruise", "Global Consumer", ""),
    "Carnival": ("CCL", "lodging_cruise", "Global Consumer", ""),
    "Flutter": ("FLUT", "leisure", "Global Consumer", ""),
    "DraftKings": ("DKNG", "leisure", "Global Consumer", ""),
    "Live Nation": ("LYV", "leisure", "Global Consumer", ""),
    "TKO": ("TKO", "leisure", "Global Consumer", ""),
    "ADM": ("ADM", "food_ag", "Global Consumer", ""),
    "Bunge": ("BG", "food_ag", "Global Consumer", ""),
    "Sysco": ("SYY", "food_ag", "Global Consumer", ""),
    "US Foods": ("USFD", "food_ag", "Global Consumer", ""),
    # US Space
    "Rocket Lab": ("RKLB", "space", "US Space", "Watch"),
    "AST SpaceMobile": ("ASTS", "space", "US Space", ""),
    "Planet Labs": ("PL", "space", "US Space", "Watch"),
    "MP Materials": ("MP", "space", "US Space", ""),
    "RBC Bearings": ("RBC", "space", "US Space", ""),
    # AI Construction
    "Quanta": ("PWR", "ai_construction", "AI Construction", "Watch"),
    "Comfort Systems": ("FIX", "ai_construction", "AI Construction", "Watch"),
    "EMCOR": ("EME", "ai_construction", "AI Construction", ""),
    "MasTec": ("MTZ", "ai_construction", "AI Construction", ""),
    "Sterling": ("STRL", "ai_construction", "AI Construction", "Watch"),
    # Homebuilder
    "Pulte": ("PHM", "homebuilder", "Homebuilder", "Watch"),
    "Lennar": ("LEN", "homebuilder", "Homebuilder", ""),
    "NVR": ("NVR", "homebuilder", "Homebuilder", ""),
    "Toll Brothers": ("TOL", "homebuilder", "Homebuilder", "Watch"),
    # China Internet
    "腾讯": ("0700.HK", "cn_mega", "China Internet", "Core"),
    "阿里巴巴": ("BABA", "cn_mega", "China Internet", "Core"),
    "拼多多": ("PDD", "cn_mega", "China Internet", "Core"),
    "网易": ("NTES", "cn_mega", "China Internet", "Core"),
    "美团": ("3690.HK", "cn_mega", "China Internet", "Core"),
    "京东": ("JD", "cn_mega", "China Internet", "Watch"),
    "百度": ("BIDU", "cn_mega", "China Internet", "Watch"),
    "携程": ("TCOM", "cn_travel_local", "China Internet", "Core"),
    "贝壳": ("BEKE", "cn_internet_small", "China Internet", "Core"),
    "希音": ("0625.HK", "cn_ecom_vertical", "China Internet", "Watch"),
    "快手": ("1024.HK", "cn_content", "China Internet", "Watch"),
    "滴滴": ("DIDIY", "cn_travel_local", "China Internet", "Watch"),
    "富途": ("FUTU", "cn_brokers_exch", "China Internet", "Core"),
    "中通": ("ZTO", "cn_logistics", "China Internet", "Watch"),
    "京东健康": ("6618.HK", "cn_health", "China Internet", "Watch"),
    "腾讯音乐": ("TME", "cn_content", "China Internet", "Watch"),
    "极兔速递": ("1519.HK", "cn_logistics", "China Internet", "Watch"),
    "京东物流": ("2618.HK", "cn_logistics", "China Internet", ""),
    "满帮": ("YMM", "cn_logistics", "China Internet", "Watch"),
    "哔哩哔哩": ("BILI", "cn_content", "China Internet", "Watch"),
    "万国数据": ("GDS", "cn_cloud_idc", "China Internet", ""),
    "阿里健康": ("0241.HK", "cn_health", "China Internet", ""),
    "京东工业": ("7618.HK", "cn_ecom_vertical", "China Internet", ""),
    "欢聚": ("JOYY", "cn_content", "China Internet", "Watch"),
    "东方甄选": ("1797.HK", "cn_ecom_vertical", "China Internet", ""),
    "同程旅行": ("0780.HK", "cn_travel_local", "China Internet", ""),
    "阅文": ("0772.HK", "cn_content", "China Internet", ""),
    "金山云": ("KC", "cn_cloud_idc", "China Internet", ""),
    "汇量科技": ("1860.HK", "games_adtech", "China Internet", "Watch"),
    "心动": ("2400.HK", "cn_games", "China Internet", "Watch"),
    "汽车之家": ("ATHM", "cn_internet_small", "China Internet", ""),
    "网易云": ("9899.HK", "cn_content", "China Internet", ""),
    "美图": ("1357.HK", "cn_content", "China Internet", ""),
    "大麦娱乐": ("1060.HK", "cn_content", "China Internet", ""),
    "赤子城": ("9911.HK", "cn_content", "China Internet", ""),
    "平安好医生": ("1833.HK", "cn_health", "China Internet", ""),
    "世纪互联": ("VNET", "cn_cloud_idc", "China Internet", ""),
    "老虎证券": ("TIGR", "cn_brokers_exch", "China Internet", ""),
    "爱回收": ("RERE", "cn_ecom_vertical", "China Internet", ""),
    "奇富": ("QFIN", "cn_consumer_finance", "China Internet", ""),
    "信也": ("FINV", "cn_consumer_finance", "China Internet", ""),
    "声网": ("API", "cn_internet_small", "China Internet", ""),
    "芒果超媒": ("300413.SZ", "cn_a_media", "China Internet", ""),
    "世纪华通": ("002602.SZ", "cn_games", "China Internet", ""),
    "巨人网络": ("002558.SZ", "cn_games", "China Internet", ""),
    "三七互娱": ("002555.SZ", "cn_games", "China Internet", ""),
    "恺英网络": ("002517.SZ", "cn_games", "China Internet", ""),
    "分众传媒": ("002027.SZ", "cn_a_media", "China Internet", ""),
    "蓝色光标": ("300058.SZ", "cn_a_media", "China Internet", ""),
    "光线传媒": ("300251.SZ", "cn_a_media", "China Internet", ""),
    # China Software
    "深演智能": ("2723.HK", "cn_ai", "China Software", ""),
    "商汤": ("0020.HK", "cn_ai", "China Software", ""),
    "迅策": ("3317.HK", "cn_ai", "China Software", ""),
    "英矽智能": ("3696.HK", "cn_ai", "China Software", "Watch"),
    "金蝶": ("0268.HK", "cn_ai", "China Software", ""),
    "迈富时": ("2556.HK", "cn_ai", "China Software", "Watch"),
    "第四范式": ("6682.HK", "cn_ai", "China Software", ""),
    "海致科技": ("2706.HK", "cn_ai", "China Software", ""),
    "滴普科技": ("1384.HK", "cn_ai", "China Software", ""),
    "金山办公": ("688111.SS", "cn_a_software", "China Software", ""),
    "科大讯飞": ("002230.SZ", "cn_a_software", "China Software", ""),
    "紫光股份": ("000938.SZ", "cn_a_software", "China Software", ""),
    "中控技术": ("688777.SS", "cn_a_software", "China Software", ""),
    "深信服": ("300454.SZ", "cn_a_software", "China Software", ""),
    "三六零": ("601360.SS", "cn_a_software", "China Software", ""),
    # China Consumer
    "安踏": ("2020.HK", "cn_consumer", "China Consumer", ""),
    "泡泡玛特": ("9992.HK", "cn_consumer", "China Consumer", "Watch"),
    "周大福": ("1929.HK", "cn_consumer", "China Consumer", ""),
    "华住": ("HTHT", "cn_travel_local", "China Consumer", "Core"),
    "老铺黄金": ("6181.HK", "cn_consumer", "China Consumer", "Watch"),
    "思摩尔": ("6969.HK", "cn_consumer", "China Consumer", ""),
    "波司登": ("3998.HK", "cn_consumer", "China Consumer", ""),
    "申洲": ("2313.HK", "cn_consumer", "China Consumer", ""),
    "布鲁可": ("0325.HK", "cn_consumer", "China Consumer", ""),
    "悦刻": ("RLX", "cn_consumer", "China Consumer", ""),
    "创想三维": ("3388.HK", "cn_consumer", "China Consumer", ""),
    # China Others
    "港交所": ("0388.HK", "cn_brokers_exch", "China Others", "Watch"),
    "中国移动": ("0941.HK", "cn_telecom", "China Others", ""),
    "中国联通": ("0762.HK", "cn_telecom", "China Others", ""),
    "中国电信": ("0728.HK", "cn_telecom", "China Others", ""),
    # Humanoid
    "宇树科技": ("688836.SS", "humanoid", "Humanoid", ""),
    "优必选": ("9880.HK", "humanoid", "Humanoid", ""),
    "三花智控": ("002050.SZ", "humanoid", "Humanoid", ""),
    "恒立液压": ("601100.SS", "humanoid", "Humanoid", "Watch"),
    "拓普集团": ("601689.SS", "humanoid", "Humanoid", ""),
    "绿的谐波": ("688017.SS", "humanoid", "Humanoid", ""),
    "敏实": ("0425.HK", "humanoid", "Humanoid", ""),
    "来福谐波": ("3952.HK", "humanoid", "Humanoid", ""),
    "Harmonic Drive": ("6324.T", "humanoid", "Humanoid", ""),
    "Nabtesco": ("6268.T", "humanoid", "Humanoid", ""),
    "Ouster": ("OUST", "humanoid", "Humanoid", ""),
    "Vishay Precision": ("VPG", "humanoid", "Humanoid", ""),
}

# Sheet rows with no market data that are companies, not section headers.
PRIVATE_ROWS = {"OpenAI", "Anthropic"}

# Extra peers / ETFs anchoring a group (tickers; may overlap with other groups)
EXTRA_PEERS: dict[str, list[str]] = {
    "space": ["NASA", "UFO"],
    "homebuilder": ["XHB", "ITB"],
    "ai_construction": ["PAVE", "VRT"],
    "bnpl": ["PYPL", "XYZ", "UPST"],
    "online_travel": ["TCOM", "TRIP"],
    "app_software": ["IGV"],
    "infra_software": ["IGV"],
    "cn_mega": ["KWEB"],
    "cn_ecom_vertical": ["BABA", "PDD", "JD"],
    "cn_travel_local": ["KWEB"],
    "cn_internet_small": ["KWEB"],
    "cn_content": ["KWEB"],
    "cn_games": ["NTES", "0700.HK"],
    "cn_brokers_exch": ["HOOD", "IBKR"],
    "cn_logistics": ["KWEB"],
    "cn_health": ["KWEB"],
}
BENCH = ["SPY", "QQQ", "KWEB", "XLF", "XLY", "IWM", "IGV", "^HSI"]
DEEP_TIERS = {"Core"}
TIERS = ("Core", "Watch")

# Flag thresholds for Watch names
WK_EXCESS_PP = 0.06
WK_Z = 2.0
DAY_EXCESS_PP = 0.05
DAY_Z = 3.0
VOL_RATIO = 2.5


def parse_sheet(path: str | Path) -> dict[str, tuple[str | None, str]]:
    """Return {name: (section, tier)} for rows tagged Core/Watch, in sheet order (first occurrence wins)."""
    with open(path, encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    section: str | None = None
    out: dict[str, tuple[str | None, str]] = {}
    for row in rows[2:]:
        if not row or not any(cell.strip() for cell in row):
            continue
        name = row[0].strip()
        cov = row[1].strip().capitalize() if len(row) > 1 else ""
        if name and not cov and not any(cell.strip() for cell in row[2:6]):
            if name not in PRIVATE_ROWS:
                section = name
            continue
        if cov in TIERS and name not in out:
            out[name] = (section, cov)
    return out


def build_universe(sheet: str | Path | None) -> tuple[dict[str, tuple[str, str, str, str]], list[dict[str, Any]]]:
    """Return (universe, unmapped). universe: name -> (ticker, group, section, tier); tier '' = peer only."""
    if not sheet:
        return dict(TICKERS), []
    tagged = parse_sheet(sheet)
    uni: dict[str, tuple[str, str, str, str]] = {}
    unmapped: list[dict[str, Any]] = []
    for name, (ticker, group, sec, _) in TICKERS.items():
        uni[name] = (ticker, group, sec, "")
    for name, (sec, tier) in tagged.items():
        if name in TICKERS:
            ticker, group, _, _ = TICKERS[name]
            uni[name] = (ticker, group, sec or TICKERS[name][2], tier)
        else:
            unmapped.append({"name": name, "section": sec, "tier": tier})
    return uni, unmapped


def covered(
    uni: dict[str, tuple[str, str, str, str]], tier: str | None = None
) -> dict[str, tuple[str, str, str, str]]:
    return {name: val for name, val in uni.items() if val[3] and (tier is None or val[3] == tier)}


def _default_download(tickers: list[str], start: str, end: str) -> Any:
    import yfinance as yf

    return yf.download(
        tickers,
        start=start,
        end=end,
        auto_adjust=False,
        group_by="ticker",
        progress=False,
        threads=True,
    )


def latest_completed_trading_day(
    cutoff_date: date,
    *,
    include_cutoff: bool = True,
    benchmark: str = "SPY",
    lookback_days: int = 21,
    downloader: Callable[[list[str], str, str], Any] | None = None,
) -> date:
    """Return latest completed US trading bar date on or before cutoff using benchmark daily bars."""
    import pandas as pd

    max_date = cutoff_date if include_cutoff else cutoff_date - timedelta(days=1)
    start_str = (max_date - timedelta(days=lookback_days)).isoformat()
    end_str = (max_date + timedelta(days=2)).isoformat()
    fetch = downloader or _default_download
    raw = fetch([benchmark], start_str, end_str)

    series = None
    if isinstance(raw, pd.Series):
        series = raw
    elif isinstance(raw, pd.DataFrame) and not raw.empty:
        if isinstance(raw.columns, pd.MultiIndex):
            if benchmark in raw.columns.get_level_values(0):
                sub = raw[benchmark]
                for col in ("Adj Close", "Close"):
                    if col in sub.columns:
                        series = sub[col]
                        break
            else:
                for col in ("Adj Close", "Close"):
                    if (col, benchmark) in raw.columns:
                        series = raw[(col, benchmark)]
                        break
        else:
            for col in ("Adj Close", "Close"):
                if col in raw.columns:
                    series = raw[col]
                    break
    if series is None:
        raise RuntimeError(f"failed to fetch {benchmark} daily bars")
    series = series.dropna()
    if series.empty:
        raise RuntimeError(f"no {benchmark} daily bars available up to {max_date.isoformat()}")
    eligible = [pd.Timestamp(idx).date() for idx in series.index if pd.Timestamp(idx).date() <= max_date]
    if not eligible:
        raise RuntimeError(f"no completed {benchmark} trading bar on or before {max_date.isoformat()}")
    return max(eligible)


def run_screen(
    uni: dict[str, tuple[str, str, str, str]],
    base: str,
    end: str,
    history_start: str,
    *,
    downloader: Callable[[list[str], str, str], Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, float | None]]:
    import numpy as np
    import pandas as pd

    base_ts, end_ts = pd.Timestamp(base), pd.Timestamp(end)
    groups: dict[str, set[str]] = {}
    for _, (ticker, group, _, _) in uni.items():
        groups.setdefault(group, set()).add(ticker)
    for group, extra in EXTRA_PEERS.items():
        if group in groups:
            groups[group].update(extra)
    tickers = sorted({ticker for vals in groups.values() for ticker in vals} | set(BENCH))

    fetch = downloader or _default_download
    dl_end = (end_ts + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    raw = fetch(tickers, history_start, dl_end)

    def series(ticker: str, field: str) -> pd.Series:
        try:
            sub = raw[ticker]
            s = sub[field].dropna()
        except (KeyError, TypeError):
            return pd.Series(dtype=float)
        return s[s.index <= end_ts]

    adj = {t: series(t, "Adj Close") for t in tickers}
    vol = {t: series(t, "Volume") for t in tickers}
    rets = {t: s.pct_change() for t, s in adj.items() if len(s)}

    def last_on_or_before(s: pd.Series, d: pd.Timestamp) -> tuple[pd.Timestamp | None, float | None]:
        sub = s[s.index <= d]
        return (sub.index[-1], float(sub.iloc[-1])) if len(sub) else (None, None)

    def window_ret(ticker: str) -> float | None:
        s = adj.get(ticker)
        if s is None or s.empty:
            return None
        d0, p0 = last_on_or_before(s, base_ts)
        d1, p1 = last_on_or_before(s, end_ts)
        if p0 is None or d0 is None or d1 is None:
            return None
        if d1 <= d0:
            return 0.0  # no trading in window (holiday)
        return p1 / p0 - 1

    out: list[dict[str, Any]] = []
    missing: list[str] = []
    for name, (ticker, group, sec, tier) in uni.items():
        if not tier:
            continue  # peer only
        s = adj.get(ticker)
        if s is None or s.empty:
            missing.append(f"{name} ({ticker})")
            continue
        peers = sorted(groups[group] - {ticker})
        w = window_ret(ticker)
        pw = [x for x in (window_ret(p) for p in peers) if x is not None]
        peer_med = float(np.median(pw)) if pw else None
        r = rets[ticker]
        peer_df = pd.DataFrame({p: rets[p] for p in peers if p in rets})
        peer_daily = peer_df.median(axis=1) if not peer_df.empty else pd.Series(dtype=float)
        exc = (r - peer_daily.reindex(r.index)).dropna()
        hist = exc[exc.index <= base_ts].tail(60)
        sd = float(hist.std()) if len(hist) > 20 else None
        v = vol[ticker]
        vhist = v[v.index <= base_ts].tail(20)
        vavg = float(vhist.mean()) if len(vhist) else None
        days: list[dict[str, Any]] = []
        for d in r.index[(r.index > base_ts) & (r.index <= end_ts)]:
            de = float(exc.loc[d]) if d in exc.index else None
            days.append(
                {
                    "date": str(d.date()),
                    "ret": float(r.loc[d]),
                    "peer": (
                        float(peer_daily.loc[d])
                        if d in peer_daily.index and not pd.isna(peer_daily.loc[d])
                        else None
                    ),
                    "excess": de,
                    "z": (de / sd) if (de is not None and sd) else None,
                    "vol_ratio": (float(v.loc[d]) / vavg) if (vavg and d in v.index) else None,
                }
            )
        wk_exc = (w - peer_med) if (w is not None and peer_med is not None) else None
        wk_z = (wk_exc / (sd * float(np.sqrt(max(len(days), 1))))) if (wk_exc is not None and sd) else None
        flags: list[dict[str, Any]] = []
        if tier not in DEEP_TIERS:
            if wk_exc is not None and (
                abs(wk_exc) >= WK_EXCESS_PP or (wk_z is not None and abs(wk_z) >= WK_Z)
            ):
                flags.append({"type": "window", "excess": wk_exc, "z": wk_z})
            for x in days:
                if x["excess"] is not None and (
                    abs(x["excess"]) >= DAY_EXCESS_PP or (x["z"] is not None and abs(x["z"]) >= DAY_Z)
                ):
                    flags.append({"type": "day", **x})
                elif x["vol_ratio"] is not None and x["vol_ratio"] >= VOL_RATIO:
                    flags.append({"type": "volume", **x})
        last_d, _ = last_on_or_before(s, end_ts)
        out.append(
            {
                "name": name,
                "ticker": ticker,
                "group": group,
                "section": sec,
                "tier": tier,
                "deep": tier in DEEP_TIERS,
                "last_date": str(last_d.date()) if last_d is not None else None,
                "peers": peers,
                "window_ret": w,
                "peer_median": peer_med,
                "window_excess": wk_exc,
                "window_z": wk_z,
                "days": days,
                "flags": flags,
            }
        )
    bench = {b: window_ret(b) for b in BENCH}
    return out, missing, bench


def build_screen_result(
    rows: list[dict[str, Any]],
    missing: list[str],
    bench: dict[str, float | None],
    unmapped: list[dict[str, Any]],
    base: str,
    end: str,
) -> dict[str, Any]:
    # Yahoo sometimes drops the current day's bar for hours after the close (returns NaN).
    # US names ending before --end mean the window silently ended a day early: warn loudly.
    us_rows = [r for r in rows if "." not in r["ticker"]]
    stale = [r["ticker"] for r in us_rows if r["last_date"] and r["last_date"] < end]
    warning = None
    if us_rows and len(stale) > len(us_rows) / 2:
        warning = (
            f"{len(stale)}/{len(us_rows)} US tickers have no bar for {end}; yfinance is missing the latest "
            f"daily bar. Rerun later or pass --end to the last complete date; do NOT report these as {end} numbers."
        )
    flagged = [r for r in rows if r["flags"]]
    flagged.sort(
        key=lambda r: -(
            abs(r["window_z"] or 0)
            + max([abs(f.get("z") or 0) for f in r["flags"]] + [0]) / 2
            + max([(f.get("vol_ratio") or 0) for f in r["flags"]] + [0]) / 2
        )
    )
    return {
        "ok": warning is None,
        "warning": warning,
        "stale_us_tickers": stale[:20],
        "base": base,
        "end": end,
        "count": len(rows),
        "core_count": sum(1 for r in rows if r["deep"]),
        "watch_count": sum(1 for r in rows if not r["deep"]),
        "missing": missing,
        "unmapped": unmapped,
        "bench": bench,
        "deep": [r for r in rows if r["deep"]],
        "flagged": flagged,
        "rows": rows,
    }


def _resolve_sheet_path(sheet_arg: str | None, workspace_dir: str | None) -> Path | None:
    ws = Path(workspace_dir) if workspace_dir else Path.cwd()
    if sheet_arg:
        return resolve_workspace_target(ws, sheet_arg)
    candidate = workspace_file(ws, "state/richard-coverage-weekly/universe.csv")
    if candidate.is_file():
        return candidate
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="Weekly price/volume screen for Richard coverage")
    ap.add_argument("command", nargs="?", default="screen", choices=["screen", "self-test"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--base", help="Base close date (YYYY-MM-DD); default end - 8 days")
    ap.add_argument("--end", help="End close date (YYYY-MM-DD); default today")
    ap.add_argument("--sheet", help="Weekly Risk Reward CSV export (optional)")
    ap.add_argument("--out", help="Write full JSON result to this file")
    ap.add_argument("--workspace-dir", default=None, help="Optional workspace root for relative paths")
    args = ap.parse_args()

    if args.command == "self-test":
        checks: dict[str, Any] = {}
        try:
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            import yfinance  # noqa: F401

            checks["imports"] = {"ok": True}
        except Exception as exc:  # noqa: BLE001
            checks["imports"] = {"ok": False, "error": str(exc), "hint": "needs yfinance/pandas/numpy in python"}
        cov = covered(TICKERS)
        tickers = [v[0] for v in TICKERS.values()]
        checks["universe"] = {
            "ok": all(len(v) == 4 and v[3] in TIERS + ("",) for v in TICKERS.values())
            and len(set(tickers)) == len(tickers)
            and len(cov) > 0,
            "mapped": len(TICKERS),
            "covered": len(cov),
            "core": len(covered(TICKERS, "Core")),
            "watch": len(covered(TICKERS, "Watch")),
        }
        ok = all(c["ok"] for c in checks.values())
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    end = args.end or date.today().isoformat()
    base = args.base or (date.fromisoformat(end) - timedelta(days=8)).isoformat()
    hist_start = (date.fromisoformat(base) - timedelta(days=140)).isoformat()
    sheet_path = _resolve_sheet_path(args.sheet, args.workspace_dir)
    uni, unmapped = build_universe(str(sheet_path) if sheet_path else None)
    try:
        rows, missing, bench = run_screen(uni, base, end, hist_start)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc), "hint": "check network / yfinance; retry once"}))
        sys.exit(1)

    result = build_screen_result(rows, missing, bench, unmapped, base, end)
    if args.out:
        out_text = json.dumps(result, ensure_ascii=False, indent=1, default=float) + "\n"
        write_target_text(Path(args.workspace_dir) if args.workspace_dir else None, args.out, out_text)
    if args.json:
        slim = {k: v for k, v in result.items() if k != "rows"}
        print(json.dumps(slim, ensure_ascii=False, default=float))
    else:
        def _pct(val: float | None) -> str:
            return "n/a" if val is None else f"{val * 100:+.1f}%"

        if result["warning"]:
            print("WARNING:", result["warning"])
        print(f"window {base} -> {end}; {len(rows)} names; missing {missing}; unmapped {[u['name'] for u in unmapped]}")
        print("CORE:")
        for r in result["deep"]:
            print(f"  {r['name']} {r['ticker']} {_pct(r['window_ret'])} vs peers {_pct(r['peer_median'])}")
        print(f"WATCH FLAGGED ({len(result['flagged'])}/{result['watch_count']}):")
        for r in result["flagged"]:
            print(f"  {r['name']} {r['ticker']} {_pct(r['window_ret'])} vs peers {_pct(r['peer_median'])} flags={len(r['flags'])}")


if __name__ == "__main__":
    main()
