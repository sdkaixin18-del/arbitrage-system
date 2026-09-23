import pytest
from app.strategy_risk import validate_card_order


def sample():
    return ({'openThreshold': 1, 'closeThreshold': 0.3, 'groupMultiplier': 200,
             'stockPerGroup': 200, 'maxGroups': 3, 'maxSlippagePct': 0.1,
             'stockAsk': 250, 'confirmedNotional': 0},
            {'unitQuantity': '1', 'groups': 200, 'action': 'open_short', 'slippagePct': '0.1'},
            {'shortPositionQuantityAbs': 200})


def test_exact_half_percent_does_not_pass():
    risk, request, account = sample()
    risk['closeThreshold'] = 0.5
    with pytest.raises(ValueError, match='0.5'):
        validate_card_order(risk, request, account)


def test_200_units_are_one_group_and_actual_position_is_capped():
    risk, request, account = sample()
    validate_card_order(risk, request, account)
    account['shortPositionQuantityAbs'] = 401
    with pytest.raises(ValueError, match='上限'):
        validate_card_order(risk, request, account)


def test_large_order_confirmation_is_bound_to_exact_notional():
    risk, request, account = sample()
    risk['stockAsk'] = 250.01
    with pytest.raises(ValueError, match='二次确认'):
        validate_card_order(risk, request, account)
    risk['confirmedNotional'] = 50002
    validate_card_order(risk, request, account)
    risk['stockAsk'] = 251
    with pytest.raises(ValueError, match='二次确认'):
        validate_card_order(risk, request, account)


def test_slippage_limit_and_close_quantity():
    risk, request, account = sample()
    request['slippagePct'] = '0.11'
    with pytest.raises(ValueError, match='滑点'):
        validate_card_order(risk, request, account)
    request.update(slippagePct='0.1', action='close_short', groups=201)
    with pytest.raises(ValueError, match='可平'):
        validate_card_order(risk, request, account)
