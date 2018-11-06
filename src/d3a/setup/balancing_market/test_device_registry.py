from d3a.models.appliance.switchable import SwitchableAppliance
from d3a.models.area import Area
from d3a.models.strategy.storage import StorageStrategy
from d3a.models.strategy.load_hours import LoadHoursStrategy, CellTowerLoadHoursStrategy
from d3a.models.appliance.pv import PVAppliance
from d3a.models.strategy.pv import PVStrategy
from d3a.d3a_core.device_registry import DeviceRegistry
from d3a.models.const import ConstSettings

device_registry_dict = {
    "H1 General Load": (32, 35),
    "H2 General Load": (32, 35),
    "H1 Storage1": (33, 35),
    "H1 Storage2": (33, 35),
}


def get_setup(config):
    DeviceRegistry.REGISTRY = device_registry_dict
    ConstSettings.BalancingSettings.ENABLE_BALANCING_MARKET = True
    area = Area(
        'Grid',
        [
            Area(
                'House 1',
                [
                    Area('H1 General Load', strategy=LoadHoursStrategy(avg_power_W=200,
                                                                       hrs_per_day=6,
                                                                       hrs_of_day=list(
                                                                           range(0, 18)),
                                                                       max_energy_rate=35),
                         appliance=SwitchableAppliance()),
                    Area('H1 Storage1', strategy=StorageStrategy(initial_capacity_kWh=0.6),
                         appliance=SwitchableAppliance()),
                    Area('H1 Storage2', strategy=StorageStrategy(initial_capacity_kWh=0.6),
                         appliance=SwitchableAppliance()),
                ]
            ),
            Area(
                'House 2',
                [
                    Area('H2 General Load', strategy=LoadHoursStrategy(avg_power_W=200,
                                                                       hrs_per_day=4,
                                                                       hrs_of_day=list(
                                                                           range(12, 16)),
                                                                       max_energy_rate=35),
                         appliance=SwitchableAppliance()),
                    Area('H2 PV', strategy=PVStrategy(4, 80),
                         appliance=PVAppliance()),

                ]
            ),
            Area('Cell Tower', strategy=CellTowerLoadHoursStrategy(avg_power_W=100,
                                                                   hrs_per_day=24,
                                                                   hrs_of_day=list(range(0, 24)),
                                                                   max_energy_rate=35),
                 appliance=SwitchableAppliance())
        ],
        config=config
    )
    return area
