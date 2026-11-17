"""将封存的共享题绑定到隔离订单、只读商品目录和上游原有工具。"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

from ecom_paired import ROOT, Shop, enable_product, enable_refund
from retailops.domain import Domain


ORDER_IDS = {
    "D01":["冻青01","冻青02","冻青03"], "D02":["湖蓝11","湖蓝12"], "D03":["云杉21"],
    "D04":["青檀31"], "D05":[], "D06":["海蓝41"], "D07":["竹影51"], "D08":["松墨61"],
    "D09":["苍蓝71"], "D13":["JD-71-A","JD-71-B"], "D14":["甲单"], "D15":["乙单"],
    "S07":["甲单","乙单"], "S08":["甲单","乙单"], "S09":["313672076398","乙单"], "S11":["甲单","乙单"],
}
CONTACTS = {"D07":("杜禾","13000001051"), "D08":("简明","13000001961"),
            "S07":("林安","13000000007"), "S08":("林安","13000000008")}
OLD, NEW = "100148801459", "100148801462"


def catalogue():
    path = ROOT/"runs/ecom-v1/upstream/envs/story/data/products.json"
    for products in json.loads(path.read_bytes())["jd"].values():
        if OLD in products and any(str(item["商品ID"]) == NEW for item in products[OLD]["可换货商品"]):
            return {pid:deepcopy(products[pid]) for pid in (OLD,NEW) if pid in products}
    raise ValueError("冻结上游没有预先登记的两款热水器")


def ecom_case(case, arm):
    uid, shop_id = "合成顾客", "合成店"
    name, phone = CONTACTS.get(case, ("测试顾客", "13000000001"))
    task = SimpleNamespace(user_id=uid, platform="jd", shop_id=shop_id)
    wiki = (ROOT/"runs/ecom-v1/upstream/wikis/agent_wiki.md").read_text()
    shop = Shop(SimpleNamespace(task=task, agent_wiki=wiki, tool_calls=[]))
    if arm != "legacy_plan":
        enable_refund(shop)
    enable_product(shop)
    shop.scoped_waits = True
    products = catalogue()
    products["860011"] = dict(商品ID="860011",商品名称="合成商品 T-71",商品状态="已上架",商品描述="型号 T-71",商品价格=168)
    products["860022"] = dict(商品ID="860022",商品名称="合成商品 T-72",商品状态="已上架",商品描述="型号 T-72",商品价格=168)
    balance = 61 if case == "S11" else 32
    status = "已签收" if case in {"D07","D08","D09","S08"} else "处理中"
    if case in {"S07","S09","S11","D14"}:
        status = "已付款"
    orders, logistics = {}, {}
    for index, oid in enumerate(ORDER_IDS[case]):
        pid = ("860011","860022","860011")[index]
        if case in {"D06","D07","D08","D09","S08","S09"} and (index==0 or case=="S08"):
            pid = OLD
        contact = dict(收件人姓名=name,电话号码=phone,详细地址="合成路8号")
        if case == "D01" and index == 2:
            contact["电话号码"] = "13000001999"
        elif case == "D06":
            contact["电话号码"] = "13000001941"
        elif case == "D03":
            contact["详细地址"] = "青柏路8号"
        amount = (239,89)[index] if case == "S11" else 168
        orders[oid] = dict(订单ID=oid,订单状态=status,支付方式="京东E卡",订单总金额=amount,
                           收货地址=contact,是否加急=False,订单商品列表=[dict(商品ID=pid,数量=1,
                           商品名称=products[pid].get("商品名称",products[pid].get("商品标题","")))])
        logistics[oid] = dict(物流状态="处理中",物流记录=[])
    if case == "D15":
        logistics["乙单"] = dict(物流状态="运输中",物流记录=["周二15:10 到达苏州配送站"])
    if case == "S09":
        orders["乙单"]["订单状态"] = "已发货"
        logistics["乙单"] = dict(物流状态="已发货",物流记录=["商家已交承运人"])
    data = dict(products={"jd":{shop_id:products}}, orders={"jd":{shop_id:{uid:orders}}},
                logistics={"jd":{shop_id:{uid:logistics}}}, invoices={}, service={},
                users_info={uid:dict(用户姓名=name,电话号码=phone,电子卡余额=balance)})
    shop.initial = lambda user: deepcopy(data)
    return shop


def tau_case(case):
    domain = Domain()
    if case != "S10":
        uid = domain.seed["orders"]["#W4386313"]["user_id"]
        return domain, [domain.seed["users"][uid]["email"]]*2
    seed = deepcopy(domain.seed)
    original = seed["orders"]["#W4386313"]
    template = seed["users"][original["user_id"]]
    contacts = []
    for index, (city, province, code) in enumerate([("杭州","浙江","310000"),("苏州","江苏","215000")], 1):
        oid = f"#W990011{index}"
        uid = f"synthetic_shared_{index}"
        email = f"shared{index}@example.invalid"
        street = f"样例巷{index}号{index}0{index}"
        address = dict(address1=street,address2="",city=city+"市",state=province+"省",zip=code,country="中国")
        user = dict(deepcopy(template),user_id=uid,email=email,orders=[oid],address=address)
        order = dict(deepcopy(original),order_id=oid,user_id=uid,address=address)
        speaker = seed["products"]["4768869376"]
        variant = speaker["variants"]["9440686670"]
        order["items"] = [dict(name=speaker["name"],product_id=speaker["product_id"],item_id=variant["item_id"],
                               price=variant["price"],options=variant["options"])]
        seed["users"][uid], seed["orders"][oid] = user, order
        contacts.append(email)
    domain.seed = seed
    from tau2.domains.retail.data_model import RetailDB
    from tau2.domains.retail.tools import RetailTools
    domain.identities = RetailTools(RetailDB(users=seed["users"], products={}, orders={}), domain.settings)
    return domain, contacts
