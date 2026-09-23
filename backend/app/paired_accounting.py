"""Fee estimates are separate from statement-confirmed settlement."""
import math

def crypto_fee(result):
    if result.get('commission') is not None and result.get('commissionAsset')=='USDT':
        fee=float(result['commission'])
        if math.isfinite(fee) and fee>=0:return fee
    return None

def estimate(job,gross,stock_entry,crypto_entry):
    c=job['config'].get('fees') or {}
    keys=('stockCommissionPct','stockMinimumCny','stockSellTaxPct','cryptoTakerPct','fundingEstimateUsdt')
    if any(type(c.get(k)) not in (int,float) for k in keys):return {'estimatedNetCny':None,'feeNote':'费率或资金费未设置'}
    sa=job['stockResult']['amount'];ca=job['cryptoResult']['executedQuantity']*job['cryptoResult']['averagePrice'];fx=gross.get('fx')
    if not fx or gross.get('totalCny') is None:return {'estimatedNetCny':None}
    # This estimate assumes one entry and one exit; split entries may incur more broker minimum fees.
    stock_fees=max(c['stockMinimumCny'],stock_entry*c['stockCommissionPct']/100)+max(c['stockMinimumCny'],sa*c['stockCommissionPct']/100)+sa*c['stockSellTaxPct']/100
    crypto_fees=(ca+crypto_entry)*c['cryptoTakerPct']/100
    return {'estimatedNetCny':gross['totalCny']-stock_fees-(crypto_fees-c['fundingEstimateUsdt'])*fx,'feeNote':'按设置费率估算，分笔最低佣金以交割单为准'}

def settle(profit,payload):
    for k in ('stockFeesCny','cryptoFeesUsdt','fundingNetUsdt'):
        v=payload.get(k)
        if type(v) not in (int,float) or not math.isfinite(v) or (k!='fundingNetUsdt' and v<0) or abs(v)>1e9:raise ValueError('请填写有效的实际费用；没有费用请明确填写0')
    if payload.get('confirm') is not True or not isinstance(payload.get('evidence'),str) or not payload['evidence'].strip():raise ValueError('请确认费用并填写交割单或账单依据')
    if profit.get('totalCny') is None or not profit.get('fx'):raise ValueError('成交成本或汇率缺失，不能确认净利润')
    net=profit['totalCny']-payload['stockFeesCny']-(payload['cryptoFeesUsdt']-payload['fundingNetUsdt'])*profit['fx']
    return {**profit,'netCny':net,'settlement':{k:payload[k] for k in ('stockFeesCny','cryptoFeesUsdt','fundingNetUsdt','evidence')},'netBasis':'账户成本净收益估算' if profit.get('estimatedCost') else '已核对费用的净收益'}
