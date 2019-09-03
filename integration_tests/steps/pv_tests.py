"""
Copyright 2018 Grid Singularity
This file is part of D3A.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
from behave import then
from math import isclose
from pendulum import duration
from d3a.models.const import ConstSettings
from d3a.constants import DEFAULT_PRECISION


@then('the storages buy energy for no more than the min PV selling rate')
def storages_pv_final_selling_rate(context):
    house1 = list(filter(lambda x: x.name == "House 1", context.simulation.area.children))[0]
    house2 = list(filter(lambda x: x.name == "House 2", context.simulation.area.children))[0]
    storage1 = list(filter(lambda x: "Storage1" in x.name, house1.children))[0]
    storage2 = list(filter(lambda x: "Storage2" in x.name, house1.children))[0]
    pv = list(filter(lambda x: "PV" in x.name, house2.children))[0]

    for market in house1.past_markets:
        for trade in market.trades:
            # Storage 2 should never buy due to break even point being lower than the
            # PV min selling rate
            assert trade.buyer != storage2.name
            if trade.buyer == storage1.name:
                # Storage 1 should buy energy offers with rate more than the PV min sell rate
                assert round(trade.offer.price / trade.offer.energy, DEFAULT_PRECISION) >= \
                       pv.strategy.offer_update.final_rate[market.time_slot]

    for market in house2.past_markets:
        assert all(trade.seller == pv.name for trade in market.trades)
        assert all(round(trade.offer.price / trade.offer.energy, DEFAULT_PRECISION) >=
                   pv.strategy.offer_update.final_rate[market.time_slot]
                   for trade in market.trades)


@then('the PV strategy decrease its sold/unsold offers price as expected')
def pv_price_decrease(context):
    house = list(filter(lambda x: x.name == "House 1", context.simulation.area.children))[0]
    pv = list(filter(lambda x: "H1 PV" in x.name, house.children))[0]

    number_of_available_updates = \
        int(pv.config.slot_length.seconds / pv.strategy.offer_update.update_interval.seconds) - 1
    energy_rate_change_per_update = 4
    rate_list = []
    for i in range(number_of_available_updates):
        rate = ConstSettings.GeneralSettings.DEFAULT_MARKET_MAKER_RATE - \
               i * energy_rate_change_per_update
        rate_list.append(rate)

    if pv.strategy.offer_update.fit_to_limit is False:
        for market in house.past_markets:
            for id, offer in market.offers.items():
                assert any([isclose(offer.price / offer.energy, rate) for rate in rate_list])
            for trade in market.trades:
                if trade.seller == pv.name:
                    assert any([isclose(trade.offer.price / trade.offer.energy,
                                rate) for rate in rate_list])

    else:
        assert False


@then("the PV offers energy as expected at an expected price")
def pv_const_energy(context):
    house = list(filter(lambda x: x.name == "House 2", context.simulation.area.children))[0]
    pv = list(filter(lambda x: "H2 PV" in x.name, house.children))[0]
    price_dec_per_slot = 0.1 * int(context.simulation.simulation_config.slot_length.seconds /
                                   context.simulation.simulation_config.tick_length.seconds)
    market_maker_rate = context.simulation.simulation_config.market_maker_rate

    for market in house.past_markets:
        slot = market.time_slot
        for id, offer in market.offers.items():
            if offer.seller == pv.name:
                assert isclose(
                    (offer.price / offer.energy),
                    market_maker_rate[slot] - price_dec_per_slot
                )
                assert isclose(offer.energy, pv.strategy.energy_production_forecast_kWh[slot] *
                               pv.strategy.panel_count, rel_tol=0.001)


@then('the load buys at most the energy equivalent of {power_W} W')
def load_buys_200_W(context, power_W):
    house1 = list(filter(lambda x: x.name == "House 1", context.simulation.area.children))[0]
    load = list(filter(lambda x: "Load" in x.name, house1.children))[0]
    max_desired_energy = float(power_W) * (house1.config.slot_length / duration(hours=1)) / 1000.0
    total_energy_per_slot = []
    for market in house1.past_markets:
        total_energy = sum(trade.offer.energy
                           for trade in market.trades
                           if trade.buyer == load.name)
        assert total_energy <= max_desired_energy
        total_energy_per_slot.append(total_energy)
    assert isclose(max(total_energy_per_slot), max_desired_energy, rel_tol=0.01)
