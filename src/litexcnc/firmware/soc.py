from migen import *
from litex.build.generic_platform import *

# Imports for creating the config
from typing import List, Type
from pydantic import BaseModel, Field, validator

# Local imports
from .encoder import EncoderConfig, EncoderModule
from .etherbone import Etherbone, EthPhy
from .gpio import GPIO, GPIO_Out, GPIO_In
from .mmio import MMIO
from .pwm import PWMConfig, PwmPdmModule
from .stepgen import StepgenConfig, StepgenModule
from .watchdog import WatchDogModule


class LitexCNC_Firmware(BaseModel):
    board_name: str = Field(
        ...,
        description="The name of the board, required for identification purposes."
    )
    baseclass: Type = Field(
        ...,
        description=""
    )
    clock_frequency: int = Field(
      50e6  
    )
    ethphy: EthPhy = Field(
        ...
    )
    etherbone: Etherbone = Field(
        ...,
    )
    gpio_in: List[GPIO] = Field(
        [],
        item_type=GPIO,
        # max_items=32,
        unique_items=True
    )
    gpio_out: List[GPIO] = Field(
        [],
        item_type=GPIO,
        # max_items=32,
        unique_items=True
    )
    pwm: List[PWMConfig] = Field(
        [],
        item_type=PWMConfig,
        unique_items=True
    )
    stepgen: List[StepgenConfig] = Field(
        [],
        item_type=StepgenConfig,
        max_items=32,
        unique_items=True
    )
    encoders: List[EncoderConfig] = Field(
        [],
        item_type=EncoderConfig,
        max_items=32,
        unique_items=True
    )

    @validator('baseclass', pre=True)
    def import_baseclass(cls, value):
        components = value.split('.')
        mod = __import__(components[0])
        print(mod)
        for comp in components[1:]:
            mod = getattr(mod, comp)
        return mod


    def generate(self, fingerprint):
        """
        Function which generates the firmware
        """
        class _LitexCNC_SoC(self.baseclass):

            def __init__(
                    self,
                    config: 'LitexCNC_Firmware',
                    ):
                
                # Configure the top level class
                super().__init__(config)

                # Create memory mapping for IO
                self.submodules.MMIO_inst = MMIO(config=config, fingerprint=fingerprint)

                # Create watchdog
                watchdog = WatchDogModule(timeout=self.MMIO_inst.watchdog_data.storage[:31], with_csr=False)
                self.submodules += watchdog
                self.sync+=[
                    # Watchdog input (fixed values)
                    watchdog.enable.eq(self.MMIO_inst.watchdog_data.storage[31]),
                    # Watchdog output (status whether the dog has bitten)
                    self.MMIO_inst.watchdog_has_bitten.status.eq(watchdog.has_bitten),
                    # self.MMIO_inst.watchdog_has_bitten.we.eq(True)
                ]

                # Create a wall-clock
                self.sync+=[
                    # Increment the wall_clock
                    self.MMIO_inst.wall_clock.status.eq(self.MMIO_inst.wall_clock.status + 1),
                    # self.MMIO_inst.wall_clock.we.eq(True)
                ]

                # Create modules
                GPIO_In.create_from_config(self, config.gpio_in)
                GPIO_Out.create_from_config(self, config.gpio_out)
                PwmPdmModule.create_from_config(self, watchdog,config.pwm)
                StepgenModule.create_from_config(self, watchdog, config.stepgen)
                EncoderModule.create_from_config(self, config.encoders)
                
        return _LitexCNC_SoC(
            config=self)
