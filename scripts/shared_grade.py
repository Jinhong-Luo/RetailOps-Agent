"""按封存题面同时核对首轮答复、草案范围、确认前账本、实际回执与终态。"""
import json
import re
from collections import Counter


def grade(case, raw):
    from shared_dialog import expected, fits
    turns=raw["轮次"]
    initial,final=raw["初态"],raw["终态"]
    receipts=raw["成功回执"]
    msgs=[t.get("面客答复","").split("[{")[0].replace("**","").replace("，",",") for t in turns]
    first=msgs[0] if msgs else ""
    unchanged=initial==final
    def plans(n):
        return turns[n].get("草案",[]) if n<len(turns) else []
    def correct(n):
        wanted=expected(case,n)
        return wanted is not None and fits(plans(n),wanted)
    def query(name):
        return any(t["function"]["name"]==name for t in raw["工具"])
    def has(text,pattern):
        return bool(re.search(pattern,text,re.I|re.S))
    def orders(data):
        return next(iter(next(iter(data["orders"]["jd"].values())).values()))
    def order(oid):
        return orders(final[0])[oid]
    def booked(oid,service,day):
        sub=final[0].get("service",{}).get("jd",{}).get("合成店",{}).get("合成顾客",{})
        r=sub.get(oid,{})
        return len(sub)==1 and r.get("服务类型")==service and r.get("预约日期")==day
    def empty_plans(n):
        return all(not plans(i) for i in range(n))
    checks={}
    if not turns:
        return {"没有可见业务轮次":False}
    if turns[0].get("执行前账本") is not None:
        checks["首次可见回复前账本未变"]=turns[0]["执行前账本"]==initial[0]
    if case=="D01":
        checks.update(首份三项正确=correct(0),只确认一次=len(receipts)==1,
                      三单分别完成=bool(order("冻青01")["是否加急"] and final[0]["invoices"].get("冻青02",{}).get("手机号码")=="13000001002" and order("冻青03")["收货地址"]["电话号码"]=="13000001003"))
    elif case=="D02":
        checks.update(首轮只办甲=correct(0),口令后只办乙=correct(1),两笔各办一次=len(receipts)==2 and all(order(i)["是否加急"] for i in ("湖蓝11","湖蓝12")))
    elif case=="D03":
        checks.update(首案两项=correct(0),改口后仅改址=correct(1),只改地址未开票=len(receipts)==1 and not final[0]["invoices"] and order("云杉21")["收货地址"]["详细地址"]=="云杉路12号")
    elif case=="D04":
        checks.update(首轮答清签收前不可退=has(first,r"签收前.{0,20}(不|无法|不能)|未签收.{0,18}(不|无法)|签收后.{0,20}(可|才|申请)|已签收.{0,22}(才|申请|可退)|只有.{0,18}签收"),
                      首份仅催送=correct(0),仅催送一次=len(receipts)==1 and order("青檀31")["是否加急"])
    elif case=="D05":
        checks.update(首轮说明支持专票=has(first,r"(支持|可以|能够|可开).{0,32}(专用发票|专票)|(专用发票|专票).{0,12}(支持|可以开具)"),
                      首轮答出原卡退款=has(first,r"退.{0,28}(京东)?E\s*卡|E\s*卡.{0,15}(收到退款|原路退)"),
                      纯咨询无草案写入=not plans(0) and unchanged)
    elif case=="D06":
        checks.update(首轮说明回收旧机补贴=has(first,r"回收|以旧换新") and has(first,r"30\s*元|三十元"),
                      首轮明确等待新号码=has(first,r"新.{0,5}(电话|号码|手机号)|提供.{0,15}(号码|电话)"),
                      首轮没有可执行草案=not plans(0))
    elif case=="D07":
        checks.update(首轮读取本账户=query("get_user_info_tool"),首份预约信息完整=correct(0),仅一条周四检查=len(receipts)==1 and booked("竹影51","检查","周四"))
    elif case=="D08":
        checks.update(前两轮等待联系人=len(turns)>=2 and empty_plans(2) and has(first,r"联系人|姓名|号码|电话"),
                      收到资料后恢复=correct(2),仅一条周一维修=len(receipts)==1 and booked("松墨61","维修","周一"))
    elif case=="D09":
        def uncertain(text):
            return has(text,r"十点|10点|十时|10[:：]00|钟点|时段|到达时间|到场时间") and has(text,r"无法|不能|未取得|未提供|还没|尚未|未.{0,4}(确认|核实|得到|收到|确定)|尚.{0,4}(无|不)|不能保证|需.{0,10}(核实|确认)|没有.{0,10}(回执|信息|答复)|不确定")
        checks.update(两次说明无法核实时刻=len(msgs)>=2 and all(uncertain(m) for m in msgs[:2]),
                      顾客暂停后不排期=len(msgs)==3 and not receipts and unchanged and empty_plans(3))
    elif case=="D10":
        checks.update(记对订单昵称="露营箱" in first,未作写入=unchanged)
    elif case=="D11":
        checks.update(礼品卡即时到账=has(first,r"(礼品卡|礼物卡|购物卡).{0,35}(立即|即时|立刻|马上|实时)"),
                      其他支付五至七个工作日=has(first,r"(5|五)\s*[-–—~至到]\s*(7|七)\s*个?工作日"),
                      原支付方式=has(first,r"原.{0,8}(支付|付款|渠道|账户|方式)|原路"),
                      指出对应取消条款=has(first,r"Cancel pending order|取消待处理订单|取消待处理的订单|待处理订单.{0,7}取消|取消未处理订单"),没有写入=unchanged)
    elif case=="D12":
        price=first.replace(",","")
        checks.update(在售价正确="2092.37" in price,款式正确="9440686670" in first or ("绿色" in first and "20" in first and "不防水" in first),没有写入=unchanged)
    elif case=="D13":
        actions=raw["工具"]
        read_list=next((n for n,t in enumerate(actions) if t["function"]["name"]=="get_user_orders_info_tool"),None)
        read_item=next((n for n,t in enumerate(actions) if t["function"]["name"]=="manage_order_tool" and
                        json.loads(t["function"]["arguments"]).get("order_id")=="JD-71-A"),None)
        checks.update(先查订单清单再核对目标明细=read_list is not None and read_item is not None and read_list<read_item,
                      首轮给出匹配订单="JD-71-A" in first and "T-71" in first,
                      未索要订单号=not has(first,r"请.{0,6}提供.{0,5}订单(号|编号)"),仅查询=unchanged and not plans(0))
    elif case=="D14":
        checks.update(首轮说明金额方向联动="168" in first and has(first,r"退.{0,25}(E卡|原卡|原支付|付款卡)|原路退") and "取消" in first,
                      首轮明确暂停=has(first,r"暂[缓停]|不(会|再|予|做|执行|办理|操作|提交)|先停|未(操作|办理|执行|取消)"),
                      避免不完整草案=not plans(0) or len(plans(0))==2,订单余额均未动=unchanged and not receipts)
    elif case=="D15":
        checks.update(末条物流时间地点正确=has(first,r"周二|星期二") and has(first,r"15[:：]10|15点10") and "苏州配送站" in first,
                      说明在途=has(first,r"在途|运输中|配送中|未签收|尚未签收|运输途中"),无额外写入=unchanged and not plans(0))
    elif case=="S07":
        needs={n["tool"] for n in turns[0].get("状态",{}).get("needs",[])}
        checks.update(分别识别两单=needs.issuperset({"manage_urgent_tool","manage_invoice_tool"}) or
                      ("甲单" in first and "乙单" in first and has(first,r"抬头")),
                      修订后仅乙单开票=correct(1),终态正确=len(receipts)==1 and not order("甲单")["是否加急"] and final[0]["invoices"].get("乙单",{}).get("发票抬头")=="青禾商贸")
    elif case=="S08":
        def waiting(n,oid,service,kind,provider):
            if n>=len(turns):
                return False
            records=turns[n].get("状态",{}).get("booking_waits",[])
            return any(w.get("order_id")==oid and w.get("service")==service and w.get("kind")==kind
                       and w.get("provider")==provider and not w.get("resolved") for w in records)
        def external(n):
            if n>=len(msgs):
                return False
            text=msgs[n]
            return "乙" in text and has(text,r"安装方|师傅|服务商|安装人员") and has(text,r"未|尚|暂|等|还没|没有|无法|待|不能") and has(text,r"时段|钟点|时间|几点|何时|到场|到达|具体")
        contact=any(e["stage"]=="查询工具" and e.get("request_id")=="0" and e.get("tool") in {"get_user_info_tool","manage_order_tool"}
                    for session in raw["事件"] for e in session)
        first_a=waiting(0,"甲单","维修","日期","顾客") or ("甲" in first and has(first,r"维修.{0,36}(日期|哪天|时间)|哪天.{0,16}维修"))
        checks.update(首轮区分等待=first_a and (waiting(0,"乙单","安装","钟点","安装方") or external(0)) and not plans(0),
                      首轮读取账户或订单联系人=contact,仅甲单周三维修=correct(1) and booked("甲单","维修","周三"),
                      甲单办理后乙单仍等安装方=len(turns)==3 and external(2) and (waiting(1,"乙单","安装","钟点","安装方") or external(1)),
                      乙单仍未约=len(receipts)==1 and len(turns)==3 and not plans(2))
    elif case=="S09":
        checks.update(首轮容量答案="14" in first and "16" in first and has(first,r"(多|增加|提升|差).{0,4}2\s*[升Ll]"),
                      首轮有效换货草案=correct(0),确认后换货一次=len(receipts)==1,
                      中段答乙单发货=len(msgs)>=2 and has(msgs[1],r"已.{0,3}(发货|交承运人|寄出|发出)"),
                      最后核实旧机补贴=len(msgs)>=3 and "30" in msgs[2] and has(msgs[2],r"回收|以旧换新") and any(
                          t["function"]["name"]=="get_gift_info_tool" and json.loads(t["function"]["arguments"]).get("product_id")=="100148801459" for t in raw["工具"]),
                      乙单只有查询=order("乙单")==orders(initial[0])["乙单"])
    elif case=="S10":
        def address_ok(index,city,part,code):
            order=next(iter(final[index]["orders"].values()));a=order["address"]
            province=("浙江","江苏")[index]
            return (city in a["city"] and province in a["state"] and "样例巷"+part in a["address1"]+a["address2"]
                    and a["zip"]==code and a["country"]==next(iter(initial[index]["orders"].values()))["address"]["country"])
        checks.update(两顾客各确认一次=len(receipts)==2,两地址正确=address_ok(0,"杭州","11号201室","310000") and address_ok(1,"苏州","22号302室","215000"),
                      乙窗口拒绝披露甲单=len(msgs)>1 and has(msgs[1],r"隐私|本人|不能|无法|无权|不便|不提供") and "11号201室" not in msgs[1] and "1号101" not in msgs[1])
    elif case=="S11":
        checks.update(首轮答清资金方向="239" in first and "退" in first and has(first,r"E\s*卡"),
                      首轮展示整组=bool(plans(0)) and fits(plans(0),expected("S11",2)),
                      第二轮再次解释且暂不执行=len(msgs)>=2 and has(msgs[1],r"退|返") and has(msgs[1],r"不|未|暂"),
                      仅甲取消和退款=len(receipts)==1 and order("甲单")["订单状态"]=="已取消" and order("乙单")==orders(initial[0])["乙单"] and final[0]["users_info"]["合成顾客"]["电子卡余额"]==300)
    elif case=="S12":
        oid="#W4386313";before=initial[0]["orders"][oid];after=final[0]["orders"][oid]
        checks.update(初次草案待确认=bool(plans(0)),乙授权取消=correct(1) and len(receipts)==1,
                      旧改址被拒=bool(turns[-1].get("拒绝")) and after["address"]==before["address"],
                      重试重放原回执=turns[-1].get("取消回执重放") is True,
                      退回原支付渠道=after["payment_history"][-1]["payment_method_id"] in {t["payment_method_id"] for t in before["payment_history"] if t["transaction_type"]=="payment"},
                      取消仅退一次=after["status"]=="cancelled" and len(after["payment_history"])==len(before["payment_history"])+1 and
                      after["payment_history"][-1]["transaction_type"]=="refund" and after["payment_history"][-1]["amount"]==3633.49)
    else:
        raise ValueError(case)
    return checks


def metrics(folder, data):
    events=[e for session in data["事件"] for e in session]
    model=[json.loads(l) for l in (folder/"模型回执.jsonl").read_text().splitlines()]
    calls=[c["function"]["name"] for r in model for c in r["消息"].get("tool_calls") or []]
    tools=[t["function"]["name"] for t in data["工具"]]
    return dict(回复轮数=len(data["轮次"]),模型响应=len(model),
                输入令牌=sum((r["用量"] or {}).get("prompt_tokens",0) for r in model),
                输出令牌=sum((r["用量"] or {}).get("completion_tokens",0) for r in model),
                响应时间合计秒=round(sum(r["秒"] for r in model),2),
                上游工具计数=dict(Counter(tools)),客服工具计数=dict(Counter(calls)),
                经验命中=[h for e in events if e["stage"]=="跨会话参考" for h in e.get("hits",[])],
                成功回执数=len(data["成功回执"]))
