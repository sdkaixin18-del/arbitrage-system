"""Checks for the five strategy-card manual order requests; no execution here."""
from decimal import Decimal


def validate_card_order(risk: dict, request: dict, account: dict) -> None:
    def number(value):
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError('风控参数不是有效数字')
        return result

    if number(risk['openThreshold']) - number(risk['closeThreshold']) <= Decimal('0.5'):
        raise ValueError('开仓阈值减去平仓阈值必须大于0.5%')
    multiplier = number(risk['groupMultiplier'])
    per_group = number(request['unitQuantity']) * multiplier
    total = number(request['unitQuantity']) * number(request['groups'])
    if multiplier not in (1, 200) or per_group <= 0:
        raise ValueError('策略组数换算错误')
    quantity = number(account.get('shortPositionQuantityAbs', abs(number(account.get('shortPositionQuantity', 0)))))
    if request.get("acceptOverCapacity"):
        raise ValueError("策略卡片不能绕过滑点容量限制")
    if number(request['slippagePct']) > number(risk['maxSlippagePct']):
        raise ValueError('下单滑点超过卡片设置')
    if request['action'] == 'open_short':
        limit = number(risk['maxGroups'])
        if limit < 1 or quantity + total > limit * per_group:
            raise ValueError('开仓后将超过卡片仓位上限')
        stock_price = number(risk['stockAsk'])
        stock_per_group = number(risk['stockPerGroup'])
        if stock_price <= 0 or stock_per_group <= 0:
            raise ValueError('A股金额无法校验')
        notional = total / per_group * stock_per_group * stock_price
        if notional > 50000 and number(risk.get('confirmedNotional', 0)) < notional:
            raise ValueError('新增A股对应金额超过5万元，需要二次确认')
    elif total > quantity:
        raise ValueError('平仓数量超过可平加密仓位')
