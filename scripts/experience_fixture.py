"""独立合成快照，限定到实验需要的上游工具，保留原业务函数。"""
from types import SimpleNamespace
from ecom_paired import Shop, TOOLS


class ExperimentShop(Shop):
    def __init__(self, number):
        self.number = number
        self.uid = f"体验顾客{number}"
        self.shop = f"体验店{number}"
        self.orders = [f"EX{number}-{i}" for i in (1, 2)]
        self.contact = f"0000000{number:04d}"
        task = SimpleNamespace(platform="jd", shop_id=self.shop, user_id=self.uid)
        policy = "当前平台{platform}，店铺{shop_id}，顾客{user_id}。用本次证据填字段，写入前须经按钮确认。"
        super().__init__(SimpleNamespace(task=task, agent_wiki=policy, tool_calls=[]))
        self.reads &= {"get_user_orders_info_tool", "get_user_info_tool", "transfer_to_specialist_tool"}
        self.writes &= {"manage_urgent_tool", "manage_invoice_tool", "schedule_service_tool"}

    def initial(self, uid):
        orders = {oid: {"订单ID": oid, "是否加急": False} for oid in self.orders}
        return {"orders": {"jd": {self.shop: {uid: orders}}}, "invoices": {},
                "users_info": {uid: {"姓名": f"测试联系人{self.number}", "电话": self.contact}}}

    def invoke(self, data, name, args):
        # 直接调用固定上游的同一业务函数。避免模块全局缓存和写盘影响并行会话。
        module = TOOLS[name].fn.__globals__
        business = module[name.removesuffix("_tool")]
        values = TOOLS[name].fn_metadata.arg_model.model_validate(args).model_dump()
        return business(data=data, **values)[1]
