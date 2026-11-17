"""用录制模型输出验证共享题脚本的入口、真实写入和错误草案不确认。"""
import asyncio
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import httpx
from shared_dialog import fits, read_account, run
from ecom_paired import Store
from shared_fixture import tau_case
from shared_dialog import prefix


async def test(root, correct):
    async def recorded(client, request, **kwargs):
        args = dict(platform="jd", shop_id="合成店",user_id="合成顾客",order_id="青檀31")
        actions = [dict(tool="manage_urgent_tool",args=args)]
        if not correct:
            actions.append(dict(tool="manage_invoice_tool",args=dict(order_id="青檀31",invoice_type="个人发票",
                                                title="合成抬头",phone_number="13000009990")))
        proposal = dict(actions=actions,consultation_reply="录制业务答复")
        msg=dict(role="assistant",content=None,tool_calls=[dict(id="候选",type="function",function=dict(
            name="propose_plan",arguments=json.dumps(proposal,ensure_ascii=False)))])
        return httpx.Response(200,request=request,json={"choices":[{"message":msg}]})
    root.mkdir()
    spec=dict(编号="D04",顾客首句="只为本人青檀31提醒派送，其他事项都不用。",后续步骤=[])
    with patch.object(httpx.AsyncClient,"send",recorded):
        data=await run(spec,"full",root)
    assert len(data["成功回执"])==int(correct)
    assert data["初态"]!=data["终态"] if correct else data["初态"]==data["终态"]
    assert len(data["轮次"])==1 and "第1版" in data["轮次"][0]["面客答复"]
    assert (root/"模型输入.jsonl").is_file() and (root/"模型回执.jsonl").is_file()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as name:
        base=Path(name)
        asyncio.run(test(base/"合法",True))
        asyncio.run(test(base/"多项",False))
        domain,emails=tau_case("D10")
        store=Store(base/"只读.db",domain)
        sid=store.identifier(store.open(emails[0]))
        initial=read_account(store,sid)
        prefix(store,sid,"D10","此处使用题库外的示例昵称。")
        assert initial==read_account(store,sid)
        assert len([m for m in store.snapshot(sid)[1] if m["role"]=="tool"])==4
    assert not fits([dict(tool="manage_order_tool",args=dict(order_id="样例",action="修改",address="新址",phone_number="额外"))],
                    [("manage_order_tool",dict(order_id="样例",action="修改",address="新址"))])
    print("真实入口正确草案单次确认、多办不确认、附加写入拦截及四轮冻结查询前缀通过")
