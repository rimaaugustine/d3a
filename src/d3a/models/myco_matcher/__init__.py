from d3a.d3a_core.util import is_external_matching_enabled
from d3a.models.myco_matcher.external_matcher import ExternalMatcher
from d3a_interface.constants_limits import ConstSettings

from d3a.models.myco_matcher.pay_as_bid import PayAsBidMatcher
from d3a.models.myco_matcher.pay_as_clear import PayAsClearMatcher
from d3a.d3a_core.exceptions import WrongMarketTypeException
from d3a_interface.enums import BidOfferMatchAlgoEnum


class MycoMatcher:
    def init(self):
        if ConstSettings.IAASettings.BID_OFFER_MATCH_TYPE == \
                BidOfferMatchAlgoEnum.PAY_AS_BID.value:
            self.match_algorithm = PayAsBidMatcher()
        elif ConstSettings.IAASettings.BID_OFFER_MATCH_TYPE == \
                BidOfferMatchAlgoEnum.PAY_AS_CLEAR.value:
            self.match_algorithm = PayAsClearMatcher()
        elif is_external_matching_enabled():
            self.match_algorithm = ExternalMatcher()
        else:
            raise WrongMarketTypeException("Wrong market type setting flag "
                                           f"{ConstSettings.IAASettings.MARKET_TYPE}")

    def calculate_recommendation(self, bids, offers, current_time):
        return self.match_algorithm.calculate_match_recommendation(
            bids, offers, current_time)
