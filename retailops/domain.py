"""装载冻结的区域数据，对原生零售工具增加会话归属和输入检查。"""
import json
import re
from collections import Counter
from functools import cached_property
from pathlib import Path

from pydantic import ValidationError
from tau2.domains.retail.data_model import RetailDB
from tau2.domains.retail.tools import RetailTools
from tau2.environment.regional import DomainRegionalSettings

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "vendor/tau2-bench/data/tau2/multilingual/zh/regional/retail.json"


class Issue(Exception):
    def __init__(self, code, reason, next_step, status=409):
        self.status = status
        self.data = dict(allowed=False, code=code, reason=reason, next_step=next_step)
        super().__init__(reason)


class Domain:
    def __init__(self, pack=PACK):
        data = json.loads(pack.read_bytes())
        self.seed = data["database"]
        self.policy = data["policies"]["policy"]
        self.settings = DomainRegionalSettings(**{k: data["settings"][k] for k in DomainRegionalSettings.model_fields})
        self.catalog = RetailDB(products=self.seed["products"], users={}, orders={}).products
        self.identities = RetailTools(RetailDB(users=self.seed["users"], products={}, orders={}), self.settings)
        self.definitions = self.identities.get_tools()
        self.writes = {name for name in self.definitions
                       if getattr(getattr(self.identities, name), "__mutates_state__", False)}
        self.reads = {"get_user_details", "get_order_details", "get_product_details",
                      "get_item_details", "list_all_product_types"}

    def initial(self, uid):
        user = self.seed["users"][uid]
        return {"users": {uid: user}, "orders": {oid: self.seed["orders"][oid] for oid in user["orders"]}}

    def valid_action_shape(self, action):
        return (isinstance(action, dict) and set(action) == {"tool", "args"}
                and isinstance(action["tool"], str) and isinstance(action["args"], dict))

    def toolkit(self, account):
        db = RetailDB(**account, products={})
        # 上游的七个写工具只修改用户和订单，商品目录供各事务只读。
        db.products = self.catalog
        return RetailTools(db, self.settings)

    def call(self, tools, uid, name, args, allowed):
        if name not in allowed:
            raise Issue("unsupported", "没有开放该工具", "选择当前支持的操作")
        fields = self.definitions[name].params
        if set(args) - set(fields.model_fields):
            raise Issue("invalid_fields", "含未知参数", "核对工具字段")
        try:
            args = fields.model_validate(args, strict=True).model_dump()
        except ValidationError as exc:
            names = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
            raise Issue("missing_or_invalid", "参数缺失或格式错误：" + names, "向用户补问相关字段") from exc
        if "user_id" in args and args["user_id"] != uid or "order_id" in args and args["order_id"] not in tools.db.orders:
            raise Issue("owner", "操作对象不属于本会话用户或不存在", "请核对本人的用户或订单编号", 403)
        for key, value in args.items():
            if (isinstance(value, list) and not value) or (isinstance(value, str) and not value.strip() and key != "address2"):
                raise Issue("missing_or_invalid", "缺少有效字段：" + key, "向用户补问相关字段")
        try:
            result = getattr(tools, name)(**args)
        except ValueError as exc:
            raise Issue("business_rule", str(exc), "核对订单状态、商品和支付条件，调整方案") from exc
        return result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    @cached_property
    def sections(self):
        headings = list(re.finditer(r"(?m)^#{1,3} (.+)$", self.policy))
        return {m[1]: self.policy[m.start():headings[i+1].start() if i+1 < len(headings) else None].strip()
                for i, m in enumerate(headings)}

    def search(self, query, limit=3):
        terms = re.findall(r"[a-z]+", query.lower())
        aliases = {"退货": "return", "退款": "refund", "换货": "exchange", "取消": "cancel",
                   "支付": "payment", "付款": "payment", "礼品卡": "gift card", "改地址": "modify address",
                   "修改商品": "modify items", "身份": "authenticate", "确认": "confirmation",
                   "库存": "available", "已送达": "delivered", "待处理": "pending"}
        for word, english in aliases.items():
            if word in query:
                terms.extend(english.split())
        scores = []
        for title, section in self.sections.items():
            counts = Counter(re.findall(r"[a-z]+", section.lower()))
            score = sum(min(counts[t], 3) + 5 * (t in title.lower().split()) for t in set(terms))
            if score:
                scores.append((score, title, section))
        return [dict(source="zh/regional/retail.json#policy/" + title, text=section)
                for _, title, section in sorted(scores, reverse=True)[:limit]]

