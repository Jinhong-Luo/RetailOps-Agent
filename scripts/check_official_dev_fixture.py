"""在锁定上游的真实只读工具中验证开发商品资料完整，禁止把夹具缺字段误判为客服失败。"""
import json
from official_memory_development import CASES,make_shop

for case in CASES:
    shop=make_shop(case)
    data=shop.initial(shop.uid)
    for pid in ("860001","860002"):
        result=json.loads(shop.invoke(data,"get_product_info_tool",dict(platform="jd",shop_id="开发店",product_id=pid)))
        assert result["商品ID"]==pid
        assert result["商品状态"]=="已上架"
        assert result["商品描述"]
    if case=="换货草案":
        result=json.loads(shop.invoke(data,"manage_exchange_tool",dict(platform="jd",shop_id="开发店",user_id=shop.uid,
            order_id="林泉741",original_product_id="860001",action="查询")))
        assert result["可换货商品"]==["860002"] and "可以进行换货" in result["订单状态"]
print("四种旁例的商品查询完整，换货资格查询通过")
