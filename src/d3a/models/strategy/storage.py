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
import traceback
from collections import namedtuple
from enum import Enum
from logging import getLogger
from typing import Union

from d3a_interface.constants_limits import ConstSettings
from d3a_interface.read_user_profile import read_arbitrary_profile, InputProfileTypes
from d3a_interface.utils import key_in_dict_and_not_none, find_object_of_same_weekday_and_time
from d3a_interface.validators import StorageValidator
from pendulum import duration

from d3a import constants
from d3a import limit_float_precision
from d3a.constants import FLOATING_POINT_TOLERANCE
from d3a.d3a_core.device_registry import DeviceRegistry
from d3a.d3a_core.exceptions import MarketException
from d3a.d3a_core.util import area_name_from_area_or_iaa_name
from d3a.models.state import StorageState, ESSEnergyOrigin, EnergyOrigin
from d3a.models.strategy import BidEnabledStrategy
from d3a.models.strategy.update_frequency import (
    TemplateStrategyOfferUpdater,
    TemplateStrategyBidUpdater)

log = getLogger(__name__)

BalancingRatio = namedtuple('BalancingRatio', ('demand', 'supply'))

StorageSettings = ConstSettings.StorageSettings
GeneralSettings = ConstSettings.GeneralSettings
BalancingSettings = ConstSettings.BalancingSettings


class StorageStrategy(BidEnabledStrategy):
    parameters = ('initial_soc', 'min_allowed_soc', 'battery_capacity_kWh',
                  'max_abs_battery_power_kW', 'cap_price_strategy', 'initial_selling_rate',
                  'final_selling_rate', 'initial_buying_rate', 'final_buying_rate', 'fit_to_limit',
                  'energy_rate_increase_per_update', 'energy_rate_decrease_per_update',
                  'update_interval', 'initial_energy_origin', 'balancing_energy_ratio')

    def __init__(self, initial_soc: float = StorageSettings.MIN_ALLOWED_SOC,
                 min_allowed_soc=StorageSettings.MIN_ALLOWED_SOC,
                 battery_capacity_kWh: float = StorageSettings.CAPACITY,
                 max_abs_battery_power_kW: float = StorageSettings.MAX_ABS_POWER,
                 cap_price_strategy: bool = False,
                 initial_selling_rate: Union[float, dict] =
                 StorageSettings.SELLING_RATE_RANGE.initial,
                 final_selling_rate: Union[float, dict] =
                 StorageSettings.SELLING_RATE_RANGE.final,
                 initial_buying_rate: Union[float, dict] =
                 StorageSettings.BUYING_RATE_RANGE.initial,
                 final_buying_rate: Union[float, dict] =
                 StorageSettings.BUYING_RATE_RANGE.final,
                 loss_per_hour=StorageSettings.LOSS_PER_HOUR,
                 loss_function=StorageSettings.LOSS_FUNCTION,
                 fit_to_limit=True, energy_rate_increase_per_update=None,
                 energy_rate_decrease_per_update=None,
                 update_interval=None,
                 initial_energy_origin: Enum = ESSEnergyOrigin.EXTERNAL,
                 balancing_energy_ratio: tuple = (BalancingSettings.OFFER_DEMAND_RATIO,
                                                  BalancingSettings.OFFER_SUPPLY_RATIO)):

        if update_interval is None:
            update_interval = \
                duration(minutes=ConstSettings.GeneralSettings.DEFAULT_UPDATE_INTERVAL)

        if min_allowed_soc is None:
            min_allowed_soc = StorageSettings.MIN_ALLOWED_SOC
        self.initial_soc = initial_soc

        StorageValidator.validate(
            initial_soc=initial_soc, min_allowed_soc=min_allowed_soc,
            battery_capacity_kWh=battery_capacity_kWh,
            max_abs_battery_power_kW=max_abs_battery_power_kW,
            loss_per_hour=loss_per_hour,
            loss_function=loss_function,
            fit_to_limit=fit_to_limit,
            energy_rate_increase_per_update=energy_rate_increase_per_update,
            energy_rate_decrease_per_update=energy_rate_decrease_per_update)

        if isinstance(update_interval, int):
            update_interval = duration(minutes=update_interval)

        BidEnabledStrategy.__init__(self)

        self.offer_update = \
            TemplateStrategyOfferUpdater(
                initial_rate=initial_selling_rate,
                final_rate=final_selling_rate,
                fit_to_limit=fit_to_limit,
                energy_rate_change_per_update=energy_rate_decrease_per_update,
                update_interval=update_interval)
        for time_slot in self.offer_update.initial_rate_profile_buffer.keys():
            StorageValidator.validate(
                initial_selling_rate=self.offer_update.initial_rate_profile_buffer[time_slot],
                final_selling_rate=find_object_of_same_weekday_and_time(
                    self.offer_update.final_rate_profile_buffer, time_slot))
        self.bid_update = \
            TemplateStrategyBidUpdater(
                initial_rate=initial_buying_rate,
                final_rate=final_buying_rate,
                fit_to_limit=fit_to_limit,
                energy_rate_change_per_update=energy_rate_increase_per_update,
                update_interval=update_interval,
                rate_limit_object=min
            )
        for time_slot in self.bid_update.initial_rate_profile_buffer.keys():
            StorageValidator.validate(
                initial_buying_rate=self.bid_update.initial_rate_profile_buffer[time_slot],
                final_buying_rate=find_object_of_same_weekday_and_time(
                    self.bid_update.final_rate_profile_buffer, time_slot))
        self.state = \
            StorageState(initial_soc=initial_soc,
                         initial_energy_origin=initial_energy_origin,
                         capacity=battery_capacity_kWh,
                         max_abs_battery_power_kW=max_abs_battery_power_kW,
                         loss_per_hour=loss_per_hour,
                         loss_function=loss_function,
                         min_allowed_soc=min_allowed_soc)
        self.cap_price_strategy = cap_price_strategy
        self.balancing_energy_ratio = BalancingRatio(*balancing_energy_ratio)

    def _area_reconfigure_prices(self, **kwargs):
        if key_in_dict_and_not_none(kwargs, 'initial_selling_rate'):
            initial_selling_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                          kwargs['initial_selling_rate'])
        else:
            initial_selling_rate = self.offer_update.initial_rate_profile_buffer
        if key_in_dict_and_not_none(kwargs, 'final_selling_rate'):
            final_selling_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                        kwargs['final_selling_rate'])
        else:
            final_selling_rate = self.offer_update.final_rate_profile_buffer
        if key_in_dict_and_not_none(kwargs, 'initial_buying_rate'):
            initial_buying_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                         kwargs['initial_buying_rate'])
        else:
            initial_buying_rate = self.bid_update.initial_rate_profile_buffer
        if key_in_dict_and_not_none(kwargs, 'final_buying_rate'):
            final_buying_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                       kwargs['final_buying_rate'])
        else:
            final_buying_rate = self.bid_update.final_rate_profile_buffer
        if key_in_dict_and_not_none(kwargs, 'energy_rate_decrease_per_update'):
            energy_rate_decrease_per_update = \
                read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                       kwargs['energy_rate_decrease_per_update'])
        else:
            energy_rate_decrease_per_update = \
                self.offer_update.energy_rate_change_per_update_profile_buffer
        if key_in_dict_and_not_none(kwargs, 'energy_rate_increase_per_update'):
            energy_rate_increase_per_update = \
                read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                       kwargs['energy_rate_increase_per_update'])
        else:
            energy_rate_increase_per_update = \
                self.bid_update.energy_rate_change_per_update_profile_buffer
        if key_in_dict_and_not_none(kwargs, 'fit_to_limit'):
            bid_fit_to_limit = kwargs['fit_to_limit']
            offer_fit_to_limit = kwargs['fit_to_limit']
        else:
            bid_fit_to_limit = self.bid_update.fit_to_limit
            offer_fit_to_limit = self.offer_update.fit_to_limit
        if key_in_dict_and_not_none(kwargs, 'update_interval'):
            if isinstance(kwargs['update_interval'], int):
                update_interval = duration(minutes=kwargs['update_interval'])
            else:
                update_interval = kwargs['update_interval']
        else:
            update_interval = self.bid_update.update_interval

        try:
            self._validate_rates(initial_selling_rate, final_selling_rate,
                                 initial_buying_rate, final_buying_rate,
                                 energy_rate_increase_per_update, energy_rate_decrease_per_update,
                                 bid_fit_to_limit, offer_fit_to_limit)
        except Exception as e:
            log.error(f"StorageStrategy._area_reconfigure_prices failed. Exception: {e}. "
                      f"Traceback: {traceback.format_exc()}")
            return

        self.offer_update.set_parameters(
            initial_rate_profile_buffer=initial_selling_rate,
            final_rate_profile_buffer=final_selling_rate,
            energy_rate_change_per_update_profile_buffer=energy_rate_decrease_per_update,
            fit_to_limit=offer_fit_to_limit,
            update_interval=update_interval
        )
        self.bid_update.set_parameters(
            initial_rate_profile_buffer=initial_buying_rate,
            final_rate_profile_buffer=final_buying_rate,
            energy_rate_change_per_update_profile_buffer=energy_rate_increase_per_update,
            fit_to_limit=bid_fit_to_limit,
            update_interval=update_interval
        )

    def area_reconfigure_event(self, **kwargs):
        """Reconfigure the device properties at runtime using the provided arguments."""
        self._area_reconfigure_prices(**kwargs)
        self._update_profiles_with_default_values()

    @staticmethod
    def _validate_rates(initial_selling_rate, final_selling_rate,
                        initial_buying_rate, final_buying_rate,
                        energy_rate_increase_per_update, energy_rate_decrease_per_update,
                        bid_fit_to_limit, offer_fit_to_limit):

        for time_slot in initial_selling_rate.keys():
            bid_rate_change = None if bid_fit_to_limit else \
                find_object_of_same_weekday_and_time(energy_rate_increase_per_update, time_slot)
            offer_rate_change = None if offer_fit_to_limit else \
                find_object_of_same_weekday_and_time(energy_rate_decrease_per_update, time_slot)
            StorageValidator.validate(
                initial_selling_rate=initial_selling_rate[time_slot],
                final_selling_rate=find_object_of_same_weekday_and_time(
                    final_selling_rate, time_slot),
                initial_buying_rate=find_object_of_same_weekday_and_time(
                    initial_buying_rate, time_slot),
                final_buying_rate=find_object_of_same_weekday_and_time(
                    final_buying_rate, time_slot),
                energy_rate_increase_per_update=bid_rate_change,
                energy_rate_decrease_per_update=offer_rate_change)

    def event_on_disabled_area(self):
        self.state.calculate_soc_for_time_slot(self.area.next_market.time_slot)

    def event_activate_price(self):
        self._validate_rates(self.offer_update.initial_rate_profile_buffer,
                             self.offer_update.final_rate_profile_buffer,
                             self.bid_update.initial_rate_profile_buffer,
                             self.bid_update.final_rate_profile_buffer,
                             self.bid_update.energy_rate_change_per_update_profile_buffer,
                             self.offer_update.energy_rate_change_per_update_profile_buffer,
                             self.bid_update.fit_to_limit, self.offer_update.fit_to_limit)

    def event_activate_energy(self):
        self.state.set_battery_energy_per_slot(self.area.config.slot_length)

    def event_activate(self, **kwargs):
        self._update_profiles_with_default_values()
        self.event_activate_energy()
        self.event_activate_price()

    def _set_alternative_pricing_scheme(self):
        if ConstSettings.IAASettings.AlternativePricing.PRICING_SCHEME != 0:
            for market in self.area.all_markets:
                time_slot = market.time_slot
                if ConstSettings.IAASettings.AlternativePricing.PRICING_SCHEME == 1:
                    self.bid_update.reassign_mixin_arguments(time_slot, initial_rate=0,
                                                             final_rate=0)
                    self.offer_update.reassign_mixin_arguments(time_slot, initial_rate=0,
                                                               final_rate=0)
                elif ConstSettings.IAASettings.AlternativePricing.PRICING_SCHEME == 2:
                    rate = \
                        self.area.config.market_maker_rate[time_slot] * \
                        ConstSettings.IAASettings.AlternativePricing.FEED_IN_TARIFF_PERCENTAGE / \
                        100
                    self.bid_update.reassign_mixin_arguments(time_slot, initial_rate=0,
                                                             final_rate=rate)
                    self.offer_update.reassign_mixin_arguments(time_slot,
                                                               initial_rate=rate,
                                                               final_rate=rate)
                elif ConstSettings.IAASettings.AlternativePricing.PRICING_SCHEME == 3:
                    rate = self.area.config.market_maker_rate[time_slot]
                    self.bid_update.reassign_mixin_arguments(time_slot, initial_rate=0,
                                                             final_rate=rate)
                    self.offer_update.reassign_mixin_arguments(time_slot,
                                                               initial_rate=rate,
                                                               final_rate=rate)
                else:
                    raise MarketException

    @staticmethod
    def _validate_constructor_arguments(initial_soc=None, min_allowed_soc=None,
                                        battery_capacity_kWh=None, max_abs_battery_power_kW=None,
                                        initial_selling_rate=None, final_selling_rate=None,
                                        initial_buying_rate=None, final_buying_rate=None,
                                        energy_rate_change_per_update=None):
        if battery_capacity_kWh is not None and battery_capacity_kWh < 0:
            raise ValueError("Battery capacity should be a positive integer")
        if max_abs_battery_power_kW is not None and max_abs_battery_power_kW < 0:
            raise ValueError("Battery Power rating must be a positive integer.")
        if initial_soc is not None and 0 < initial_soc > 100:
            raise ValueError("initial SOC must be in between 0-100 %")
        if min_allowed_soc is not None and 0 < min_allowed_soc > 100:
            raise ValueError("initial SOC must be in between 0-100 %")
        if initial_soc is not None and min_allowed_soc is not None and \
                initial_soc < min_allowed_soc:
            raise ValueError("Initial charge must be more than the minimum allowed soc.")
        if initial_selling_rate is not None and initial_selling_rate < 0:
            raise ValueError("Initial selling rate must be greater equal 0.")
        if final_selling_rate is not None:
            if type(final_selling_rate) is float and final_selling_rate < 0:
                raise ValueError("Final selling rate must be greater equal 0.")
            elif type(final_selling_rate) is dict and \
                    any(rate < 0 for _, rate in final_selling_rate.items()):
                raise ValueError("Final selling rate must be greater equal 0.")
        if initial_selling_rate is not None and final_selling_rate is not None:
            initial_selling_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                          initial_selling_rate)
            final_selling_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                        final_selling_rate)
            if any(initial_selling_rate[hour] < final_selling_rate[hour]
                   for hour, _ in initial_selling_rate.items()):
                raise ValueError("Initial selling rate must be greater than final selling rate.")
        if initial_buying_rate is not None and initial_buying_rate < 0:
            raise ValueError("Initial buying rate must be greater equal 0.")
        if final_buying_rate is not None:
            final_buying_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                       final_buying_rate)
            if any(rate < 0 for _, rate in final_buying_rate.items()):
                raise ValueError("Final buying rate must be greater equal 0.")
        if initial_buying_rate is not None and final_buying_rate is not None:
            initial_buying_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                         initial_buying_rate)
            final_buying_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                       final_buying_rate)
            if any(initial_buying_rate[hour] > final_buying_rate[hour]
                   for hour, _ in initial_buying_rate.items()):
                raise ValueError("Initial buying rate must be less than final buying rate.")
        if final_selling_rate is not None and final_buying_rate is not None:
            final_selling_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                        final_selling_rate)
            final_buying_rate = read_arbitrary_profile(InputProfileTypes.IDENTITY,
                                                       final_buying_rate)
            if any(final_buying_rate[hour] >= final_selling_rate[hour]
                   for hour, _ in final_selling_rate.items()):
                raise ValueError("final_buying_rate should be higher than final_selling_rate.")
        if energy_rate_change_per_update is not None and energy_rate_change_per_update < 0:
            raise ValueError("energy_rate_change_per_update should be a non-negative value.")

    def event_tick(self):
        """Post bids or update existing bid prices on market tick.

        This method is triggered by the TICK event.
        """
        self.state.clamp_energy_to_buy_kWh(self.future_markets_time_slots)

        for market in self.area.all_markets:
            if ConstSettings.IAASettings.MARKET_TYPE == 2 or \
                    ConstSettings.IAASettings.MARKET_TYPE == 3:
                self.state.clamp_energy_to_buy_kWh(self.future_markets_time_slots)
                if self.are_bids_posted(market.id):
                    self.bid_update.update(market, self)
                else:
                    energy_kWh = self.state.energy_to_buy_dict[market.time_slot]
                    if energy_kWh > 0:
                        try:
                            first_bid = self.post_first_bid(market, energy_kWh * 1000.0)
                            if first_bid is not None:
                                self.state.offered_buy_kWh[market.time_slot] += first_bid.energy
                        except MarketException:
                            pass

            self.state.tick(self.area, market.time_slot)
        if self.cap_price_strategy is False:
            self.offer_update.update(self)

        self.bid_update.increment_update_counter_all_markets(self)
        if self.offer_update.increment_update_counter_all_markets(self):
            for market in self.area.all_markets:
                self.buy_energy(market)

    def event_trade(self, *, market_id, trade):
        market = self.area.get_future_market_from_id(market_id)
        super().event_trade(market_id=market_id, trade=trade)

        self.assert_if_trade_bid_price_is_too_high(market, trade)
        self.assert_if_trade_offer_price_is_too_low(market_id, trade)

        if trade.buyer == self.owner.name:
            if ConstSettings.IAASettings.MARKET_TYPE == 1:
                # in order to omit double counting this is only applied for one sided market
                self._track_energy_bought_type(trade)
        if trade.seller == self.owner.name:
            self._track_energy_sell_type(trade)
            self.state.pledged_sell_kWh[market.time_slot] += trade.offer.energy
            self.state.offered_sell_kWh[market.time_slot] -= trade.offer.energy

    def _is_local(self, trade):
        for child in self.area.children:
            if child.name == trade.seller:
                return True
        return False

    # ESS Energy being utilized based on FIRST-IN FIRST-OUT mechanism
    def _track_energy_sell_type(self, trade):
        energy = trade.offer.energy
        while limit_float_precision(energy) > 0 and len(self.state.get_used_storage_share) > 0:
            first_in_energy_with_origin = self.state.get_used_storage_share[0]
            if energy >= first_in_energy_with_origin.value:
                energy -= first_in_energy_with_origin.value
                self.state.get_used_storage_share.pop(0)
            elif energy < first_in_energy_with_origin.value:
                residual = first_in_energy_with_origin.value - energy
                self.state._used_storage_share[0] = \
                    EnergyOrigin(first_in_energy_with_origin.origin, residual)
                energy = 0

    def _track_energy_bought_type(self, trade):
        if area_name_from_area_or_iaa_name(trade.seller) == self.area.name:
            self.state.update_used_storage_share(trade.offer.energy, ESSEnergyOrigin.EXTERNAL)
        elif self._is_local(trade):
            self.state.update_used_storage_share(trade.offer.energy, ESSEnergyOrigin.LOCAL)
        else:
            self.state.update_used_storage_share(trade.offer.energy, ESSEnergyOrigin.UNKNOWN)

    def event_bid_traded(self, *, market_id, bid_trade):
        super().event_bid_traded(market_id=market_id, bid_trade=bid_trade)
        market = self.area.get_future_market_from_id(market_id)

        if bid_trade.buyer == self.owner.name:
            self._track_energy_bought_type(bid_trade)
            self.state.pledged_buy_kWh[market.time_slot] += bid_trade.offer.energy
            self.state.offered_buy_kWh[market.time_slot] -= bid_trade.offer.energy

    def event_market_cycle(self):
        super().event_market_cycle()
        self._set_alternative_pricing_scheme()
        self._update_profiles_with_default_values()
        self.offer_update.reset(self)
        for market in self.area.all_markets[:-1]:
            self.bid_update.update_counter[market.time_slot] = 0
        current_market = self.area.next_market
        past_market = self.area.last_past_market

        self.state.market_cycle(
            past_market.time_slot if past_market else None,
            current_market.time_slot,
            self.future_markets_time_slots
        )

        if self.state.used_storage > 0:
            self.sell_energy()

        if ConstSettings.IAASettings.MARKET_TYPE == 2 or \
           ConstSettings.IAASettings.MARKET_TYPE == 3:
            self.state.clamp_energy_to_buy_kWh([current_market.time_slot])
            self.bid_update.reset(self)
            energy_kWh = self.state.energy_to_buy_dict[current_market.time_slot]
            if energy_kWh > 0:
                try:
                    self.post_first_bid(current_market, energy_kWh * 1000.0)
                    self.state.offered_buy_kWh[current_market.time_slot] += energy_kWh
                except MarketException:
                    pass

        self._delete_past_state()

    def event_balancing_market_cycle(self):
        if not self.is_eligible_for_balancing_market:
            return

        current_market = self.area.next_market
        free_storage = self.state.free_storage(current_market.time_slot)
        if free_storage > 0:
            charge_energy = self.balancing_energy_ratio.demand * free_storage
            charge_price = DeviceRegistry.REGISTRY[self.owner.name][0] * charge_energy
            if charge_energy != 0 and charge_price != 0:
                # committing to start charging when required
                self.area.get_balancing_market(self.area.now).balancing_offer(charge_price,
                                                                              -charge_energy,
                                                                              self.owner.name)
        if self.state.used_storage > 0:
            discharge_energy = self.balancing_energy_ratio.supply * self.state.used_storage
            discharge_price = DeviceRegistry.REGISTRY[self.owner.name][1] * discharge_energy
            # committing to start discharging when required
            if discharge_energy != 0 and discharge_price != 0:
                self.area.get_balancing_market(self.area.now).balancing_offer(discharge_price,
                                                                              discharge_energy,
                                                                              self.owner.name)

    def _try_to_buy_offer(self, offer, market, max_affordable_offer_rate):
        if offer.seller == self.owner.name:
            # Don't buy our own offer
            return
        # Check if the price is cheap enough
        if offer.energy_rate > max_affordable_offer_rate:
            # Can early return here, because the offers are sorted according to energy rate
            # therefore the following offers will be more expensive
            return True
        alt_pricing_settings = ConstSettings.IAASettings.AlternativePricing
        if offer.seller == alt_pricing_settings.ALT_PRICING_MARKET_MAKER_NAME \
                and alt_pricing_settings.PRICING_SCHEME != 0:
            # don't buy from IAA if alternative pricing scheme is activated
            return

        try:
            self.state.clamp_energy_to_buy_kWh([ma.time_slot for ma in self.area.all_markets])
            max_energy = min(offer.energy, self.state.energy_to_buy_dict[market.time_slot])
            if not self.state.has_battery_reached_max_power(-max_energy, market.time_slot):
                self.state.pledged_buy_kWh[market.time_slot] += max_energy
                self.accept_offer(market, offer, energy=max_energy,
                                  buyer_origin=self.owner.name,
                                  buyer_origin_id=self.owner.uuid,
                                  buyer_id=self.owner.uuid)
            return
        except MarketException:
            # Offer already gone etc., try next one.
            return

    def buy_energy(self, market, offer=None):
        if not market:
            return
        if self.state.has_battery_reached_max_power(-FLOATING_POINT_TOLERANCE, market.time_slot):
            return
        max_affordable_offer_rate = self.bid_update.get_updated_rate(market.time_slot)
        # Check if storage has free capacity
        if self.state.free_storage(market.time_slot) <= 0.0:
            return

        if offer:
            self._try_to_buy_offer(offer, market, max_affordable_offer_rate)
        else:
            for offer in market.sorted_offers:
                if self._try_to_buy_offer(offer, market, max_affordable_offer_rate) is False:
                    return

    def sell_energy(self):
        markets_to_sell = self.select_market_to_sell()
        energy_sell_dict = self.state.clamp_energy_to_sell_kWh(
            [ma.time_slot for ma in markets_to_sell])
        for market in markets_to_sell:
            selling_rate = self.calculate_selling_rate(market)
            energy = energy_sell_dict[market.time_slot]
            if not self.state.has_battery_reached_max_power(energy, market.time_slot):
                if energy > 0.0:
                    try:
                        offer = market.offer(
                            price=energy * selling_rate,
                            energy=energy,
                            seller=self.owner.name,
                            original_offer_price=energy * selling_rate,
                            seller_origin=self.owner.name,
                            seller_origin_id=self.owner.uuid,
                            seller_id=self.owner.uuid
                        )
                        self.offers.post(offer, market.id)
                        self.state.offered_sell_kWh[market.time_slot] += offer.energy
                    except MarketException:
                        pass

    def select_market_to_sell(self):
        if StorageSettings.SELL_ON_MOST_EXPENSIVE_MARKET:
            # Sell on the most expensive market
            try:
                max_rate = 0.0
                most_expensive_market = self.area.all_markets[0]
                for market in self.area.all_markets:
                    if len(market.sorted_offers) > 0 and \
                       market.sorted_offers[0].energy_rate > max_rate:
                        max_rate = market.sorted_offers[0].price / market.sorted_offers[0].energy
                        most_expensive_market = market
            except IndexError:
                try:
                    most_expensive_market = self.area.current_market
                except StopIteration:
                    return
            return [most_expensive_market]
        else:
            return self.area.all_markets

    def calculate_selling_rate(self, market):
        if self.cap_price_strategy is True:
            return self.capacity_dependant_sell_rate(market)
        else:
            return self.offer_update.initial_rate[market.time_slot]

    def capacity_dependant_sell_rate(self, market):
        if self.state.charge_history[market.time_slot] == "-":
            soc = self.state.used_storage / self.state.capacity
        else:
            soc = self.state.charge_history[market.time_slot] / 100.0
        max_selling_rate = self.offer_update.initial_rate[market.time_slot]
        min_selling_rate = self.offer_update.final_rate[market.time_slot]
        if max_selling_rate < min_selling_rate:
            return min_selling_rate
        else:
            return max_selling_rate - (max_selling_rate - min_selling_rate) * soc

    def _update_profiles_with_default_values(self):
        self.offer_update.update_and_populate_price_settings(self.area)
        self.bid_update.update_and_populate_price_settings(self.area)
        self.state.add_default_values_to_state_profiles(self.future_markets_time_slots)

    def event_offer(self, *, market_id, offer):
        super().event_offer(market_id=market_id, offer=offer)
        if ConstSettings.IAASettings.MARKET_TYPE == 1:
            market = self.area.get_future_market_from_id(market_id)
            # sometimes the offer event arrives earlier than the market_cycle event,
            # so the default values have to be written here too:
            self._update_profiles_with_default_values()
            if offer.id in market.offers and \
                    offer.seller != self.owner.name and \
                    offer.seller != self.area.name:
                self.buy_energy(market, offer)

    def _delete_past_state(self):
        if constants.D3A_TEST_RUN is True or \
                self.area.current_market is None:
            return

        self.offer_update.delete_past_state_values(self.area.current_market.time_slot)
        self.bid_update.delete_past_state_values(self.area.current_market.time_slot)
        self.state.delete_past_state_values(self.area.current_market.time_slot)
