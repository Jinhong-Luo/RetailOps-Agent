"""整题运行可在短暂路由中断后重送当前请求，所有尝试共用原预算。"""
import asyncio

from retailops.memory_budget import BudgetStopped


ROUTE_STOP = "上游工作区路由不可用，保留预留并停止"


def reopen_route(ledger, failed_call):
    """仅允许恢复最后一笔已记录的路由失败，保留旧请求预留及原时限。"""
    with ledger.transaction() as db:
        stop, = db.execute("select stop from run where id=1").fetchone()
        last = db.execute("select id,used,status from calls order by id desc limit 1").fetchone()
        if stop != ROUTE_STOP or last != (failed_call, None, "HTTP 502"):
            raise BudgetStopped(stop or "请求状态变化，暂停恢复")
        db.execute("update run set stop=null where id=1")


async def deliver(target, request, ledger, name, payload, retry_route):
    for attempt in range(3):
        call = ledger.reserve(name, payload)
        try:
            response = await target.handle_async_request(request)
            await response.aread()
        except BaseException:
            ledger.finish(call)
            raise
        ledger.finish(call, response)
        if response.is_success:
            return call, response
        reason = ledger.summary()["停止原因"]
        status = response.status_code
        await response.aclose()
        if not retry_route or reason != ROUTE_STOP or status != 502 or attempt == 2:
            raise BudgetStopped(reason)
        print(f"请求 {call} 工作区路由中断，保留预算预留并等待恢复", flush=True)
        await asyncio.sleep(30 * (attempt + 1))
        reopen_route(ledger, call)
