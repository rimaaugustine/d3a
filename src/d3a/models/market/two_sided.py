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
import uuid
from dataclasses import replace
from logging import getLogger
from math import isclose
from typing import Union  # noqa

from d3a_interface.constants_limits import ConstSettings

from d3a.d3a_core.exceptions import BidNotFound, InvalidBid, InvalidTrade, MarketException
from d3a.d3a_core.util import short_offer_bid_log_str
from d3a.events.event_structures import MarketEvent
from d3a.models.market import lock_market_action, validate_authentic_bid_offer_pair
from d3a.models.market.market_structures import Bid, Trade, TradeBidOfferInfo
from d3a.models.market.one_sided import OneSidedMarket

log = getLogger(__name__)


class TwoSidedMarket(OneSidedMarket):

    def __init__(self, time_slot=None, bc=None, notification_listener=None, readonly=False,
                 grid_fee_type=ConstSettings.IAASettings.GRID_FEE_TYPE,
                 grid_fees=None, name=None, in_sim_duration=True):
        super().__init__(time_slot, bc, notification_listener, readonly, grid_fee_type,
                         grid_fees, name, in_sim_duration=in_sim_duration)

    def __repr__(self):  # pragma: no cover
        return "<TwoSidedPayAsBid{} bids: {} (E: {} kWh V:{}) " \
               "offers: {} (E: {} kWh V: {}) trades: {} (E: {} kWh, V: {})>"\
            .format(" {}".format(self.time_slot_str),
                    len(self.bids),
                    sum(b.energy for b in self.bids.values()),
                    sum(b.price for b in self.bids.values()),
                    len(self.offers),
                    sum(o.energy for o in self.offers.values()),
                    sum(o.price for o in self.offers.values()),
                    len(self.trades),
                    self.accumulated_trade_energy,
                    self.accumulated_trade_price
                    )

    def _update_new_bid_price_with_fee(self, bid_price, original_bid_price):
        return self.fee_class.update_incoming_bid_with_fee(bid_price, original_bid_price)

    @lock_market_action
    def get_bids(self):
        return self.bids

    @lock_market_action
    def bid(self, price: float, energy: float, buyer: str, buyer_origin,
            bid_id: str = None, original_bid_price=None, adapt_price_with_fees=True,
            add_to_history=True, buyer_origin_id=None, buyer_id=None) -> Bid:
        if energy <= 0:
            raise InvalidBid()

        if original_bid_price is None:
            original_bid_price = price

        if adapt_price_with_fees:
            price = self._update_new_bid_price_with_fee(price, original_bid_price)

        if price < 0.0:
            raise MarketException("Negative price after taxes, bid cannot be posted.")

        bid = Bid(str(uuid.uuid4()) if bid_id is None else bid_id,
                  self.now, price, energy, buyer, original_bid_price, buyer_origin,
                  buyer_origin_id=buyer_origin_id, buyer_id=buyer_id)

        self.bids[bid.id] = bid
        if add_to_history is True:
            self.bid_history.append(bid)
        log.debug(f"[BID][NEW][{self.time_slot_str}] {bid}")
        return bid

    @lock_market_action
    def delete_bid(self, bid_or_id: Union[str, Bid]):
        if isinstance(bid_or_id, Bid):
            bid_or_id = bid_or_id.id
        bid = self.bids.pop(bid_or_id, None)
        if not bid:
            raise BidNotFound(bid_or_id)
        log.debug(f"[BID][DEL][{self.time_slot_str}] {bid}")
        self._notify_listeners(MarketEvent.BID_DELETED, bid=bid)

    def split_bid(self, original_bid, energy, orig_bid_price):

        self.bids.pop(original_bid.id, None)
        # same bid id is used for the new accepted_bid
        original_accepted_price = energy / original_bid.energy * orig_bid_price
        accepted_bid = self.bid(bid_id=original_bid.id,
                                price=original_bid.price * (energy / original_bid.energy),
                                energy=energy,
                                buyer=original_bid.buyer,
                                original_bid_price=original_accepted_price,
                                buyer_origin=original_bid.buyer_origin,
                                buyer_origin_id=original_bid.buyer_origin_id,
                                buyer_id=original_bid.buyer_id,
                                adapt_price_with_fees=False,
                                add_to_history=False)

        residual_price = (1 - energy / original_bid.energy) * original_bid.price
        residual_energy = original_bid.energy - energy

        original_residual_price = \
            ((original_bid.energy - energy) / original_bid.energy) * orig_bid_price

        residual_bid = self.bid(price=residual_price,
                                energy=residual_energy,
                                buyer=original_bid.buyer,
                                original_bid_price=original_residual_price,
                                buyer_origin=original_bid.buyer_origin,
                                buyer_origin_id=original_bid.buyer_origin_id,
                                buyer_id=original_bid.buyer_id,
                                adapt_price_with_fees=False,
                                add_to_history=True)

        log.debug(f"[BID][SPLIT][{self.time_slot_str}, {self.name}] "
                  f"({short_offer_bid_log_str(original_bid)} into "
                  f"{short_offer_bid_log_str(accepted_bid)} and "
                  f"{short_offer_bid_log_str(residual_bid)}")

        self._notify_listeners(MarketEvent.BID_SPLIT,
                               original_bid=original_bid,
                               accepted_bid=accepted_bid,
                               residual_bid=residual_bid)

        return accepted_bid, residual_bid

    def determine_bid_price(self, trade_offer_info, energy):
        revenue, grid_fee_rate, final_trade_rate = \
            self.fee_class.calculate_trade_price_and_fees(trade_offer_info)
        return grid_fee_rate * energy, energy * final_trade_rate

    @lock_market_action
    def accept_bid(self, bid: Bid, energy: float = None,
                   seller: str = None, buyer: str = None, already_tracked: bool = False,
                   trade_rate: float = None, trade_offer_info=None, seller_origin=None,
                   seller_origin_id=None, seller_id=None):
        market_bid = self.bids.pop(bid.id, None)
        if market_bid is None:
            raise BidNotFound("During accept bid: " + str(bid))

        buyer = market_bid.buyer if buyer is None else buyer

        if energy is None or isclose(energy, market_bid.energy, abs_tol=1e-8):
            energy = market_bid.energy

        orig_price = bid.original_bid_price if bid.original_bid_price is not None else bid.price
        residual_bid = None

        if energy <= 0:
            raise InvalidTrade("Energy cannot be negative or zero.")
        elif energy > market_bid.energy:
            raise InvalidTrade(f"Traded energy ({energy}) cannot be more than the "
                               f"bid energy ({market_bid.energy}).")
        elif energy < market_bid.energy:
            # partial bid trade
            accepted_bid, residual_bid = self.split_bid(market_bid, energy, orig_price)
            bid = accepted_bid

            # Delete the accepted bid from self.bids:
            try:
                self.bids.pop(accepted_bid.id)
            except KeyError:
                raise BidNotFound(f"Bid {accepted_bid.id} not found in self.bids ({self.name}).")
        else:
            # full bid trade, nothing further to do here
            pass

        fee_price, trade_price = self.determine_bid_price(trade_offer_info, energy)
        bid = replace(bid, price=trade_price)

        # Do not adapt grid fees when creating the bid_trade_info structure, to mimic
        # the behavior of the forwarded bids which use the source market fee.
        updated_bid_trade_info = self.fee_class.propagate_original_offer_info_on_bid_trade(
            trade_offer_info, ignore_fees=True
        )

        trade = Trade(str(uuid.uuid4()), self.now, bid, seller,
                      buyer, residual_bid, already_tracked=already_tracked,
                      offer_bid_trade_info=updated_bid_trade_info,
                      buyer_origin=bid.buyer_origin, seller_origin=seller_origin,
                      fee_price=fee_price, seller_origin_id=seller_origin_id,
                      buyer_origin_id=bid.buyer_origin_id, seller_id=seller_id,
                      buyer_id=bid.buyer_id
                      )

        if already_tracked is False:
            self._update_stats_after_trade(trade, bid, already_tracked)
            log.info(f"[TRADE][BID] [{self.name}] [{self.time_slot_str}] {trade}")

        self._notify_listeners(MarketEvent.BID_TRADED, bid_trade=trade)
        return trade

    def accept_bid_offer_pair(self, bid, offer, clearing_rate, trade_bid_info, selected_energy):
        validate_authentic_bid_offer_pair(bid, offer, clearing_rate, selected_energy)
        already_tracked = bid.buyer == offer.seller
        trade = self.accept_offer(offer_or_id=offer,
                                  buyer=bid.buyer,
                                  energy=selected_energy,
                                  trade_rate=clearing_rate,
                                  already_tracked=already_tracked,
                                  trade_bid_info=trade_bid_info,
                                  buyer_origin=bid.buyer_origin,
                                  buyer_origin_id=bid.buyer_origin_id,
                                  buyer_id=bid.buyer_id)

        bid_trade = self.accept_bid(bid=bid,
                                    energy=selected_energy,
                                    seller=offer.seller,
                                    buyer=bid.buyer,
                                    already_tracked=True,
                                    trade_rate=clearing_rate,
                                    trade_offer_info=trade_bid_info,
                                    seller_origin=offer.seller_origin,
                                    seller_origin_id=offer.seller_origin_id,
                                    seller_id=offer.seller_id)
        return bid_trade, trade

    def match_offers_bids(self):
        pass

    def match_recommendation(self, recommended_list):
        if recommended_list is None:
            return
        for index, recommended_pair in enumerate(recommended_list):

            selected_energy = recommended_pair.selected_energy
            bid = recommended_pair.bid
            offer = recommended_pair.offer
            original_bid_rate = \
                bid.original_bid_price / bid.energy

            trade_bid_info = TradeBidOfferInfo(
                original_bid_rate=original_bid_rate,
                propagated_bid_rate=bid.price/bid.energy,
                original_offer_rate=offer.original_offer_price/offer.energy,
                propagated_offer_rate=offer.price/offer.energy,
                trade_rate=original_bid_rate)

            bid_trade, trade = self.accept_bid_offer_pair(
                bid, offer, recommended_pair.trade_rate, trade_bid_info, selected_energy
            )

            if trade.residual is not None or bid_trade.residual is not None:
                recommended_list = self._replace_offers_bids_with_residual_in_matching_list(
                    recommended_list, index+1, trade, bid_trade
                )

    @classmethod
    def _replace_offers_bids_with_residual_in_matching_list(
            cls, matchings, start_index, offer_trade, bid_trade
    ):
        def _convert_match_to_residual(match):
            if match.offer.id == offer_trade.offer.id:
                assert offer_trade.residual is not None
                match = replace(match, offer=offer_trade.residual)
            if match.bid.id == bid_trade.offer.id:
                assert bid_trade.residual is not None
                match = replace(match, bid=bid_trade.residual)
            return match

        matchings[start_index:] = [_convert_match_to_residual(match)
                                   for match in matchings[start_index:]]
        return matchings
