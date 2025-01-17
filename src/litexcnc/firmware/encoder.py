from typing import List
import math
import warnings

# Imports for creating a json-definition
from pydantic import BaseModel, Field, root_validator

# Imports for creating a LiteX/Migen module
from litex.soc.interconnect.csr import *
from migen import *
from litex.soc.integration.soc import SoC
from litex.soc.integration.doc import AutoDoc, ModuleDoc
from litex.build.generic_platform import *


class EncoderConfig(BaseModel):
    """Configuration for hardware counting of quadrature encoder signals."""
    name: str = Field(
        None,
        description="The name of the encoder as used in LinuxCNC HAL-file (optional). "
    )
    pin_A: str = Field(
        description="The pin on the FPGA-card for Encoder A-signal."
    )
    pin_B: str = Field(
        description="The pin on the FPGA-card for Encoder B-signal."
    )
    pin_Z: str = Field(
        None,
        description="The pin on the FPGA-card for Encoder Z-signal. This pin is optional, "
    "when not set the Z-pulse register on the FPGA will not be set, but it will be created. "
    "In the driver the phase-Z bit will not be exported and the function `index-enable` will "
    "be disabled when there is no Z-signal."
    )
    min_value: int = Field(
        None,
        description="The minimum value for the encoder. Extra pulses will not cause the counter"
        "to decrease beyond this value. This value is inclusive. When the value is not defined, the "
        "minimum value is unlimited. The minimum value should be smaller then the maximum "
        "value if both are defined."
    )
    max_value: int = Field(
        None,
        description="The maximum value for the encoder. Extra pulses will not cause the counter"
        "to increase beyond this value. This value is inclusive. When the value is not defined, the "
        "maximum value is unlimited. The maximum value should be larger then the minimum "
        "value if both are defined."
    )
    reset_value: int = Field(
        0,
        description="The value to which the counter will be resetted. This is also the initial value "
        "at which the counter is instantiated. The reset value should be between the minimum value "
        "and maximum value if these are defined. Default value: 0."
        "NOTE: when the encoder has X4 set to False, the value reported in HAL will be this value "
        "divided by 4."
    )
    io_standard: str = Field(
        "LVCMOS33",
        description="The IO Standard (voltage) to use for the pins."
    )

    @root_validator(skip_on_failure=True)
    def check_min_max_reset_value(cls, values):
        """
        Checks whether the min value is smaller then max value and that the
        reset value is larger then the minimum value, but smaller then the
        maximum value.
        """
        reset_value = values.get('reset_value')
        min_value = values.get('min_value', None)
        # Check the reset value relative to the minimum value if the latter is defined
        if min_value is not None:
            if reset_value < min_value:
                raise ValueError('Reset value should be larger then or equal to the minimum value.')
        max_value = values.get('max_value', None)
        # Check the reset value relative to the maximum value if the latter is defined
        if max_value is not None:
            if reset_value > max_value:
                raise ValueError('Reset value should be smaller then or equal to the minimum value.')
        # If both minimum and maximum values are defined, check whether they are in the
        # correct order. Technically it is possible to have minimum and maximum value
        # equal to each other, however this will result in a counter which is not working
        # (fixed at one value). In this case we warn the user that the counter won't work.
        if min_value is not None and max_value is not None:
            if max_value < min_value:
                raise ValueError('Minimum value should be smaller then the maximum value.')
            if max_value == min_value:
                warnings.warn('Minimum and maximum value are equal! The counter will not work '
                'because its value is fixed. It is recommended to change the values.')
        # Everything OK, pass on the values
        return values


class EncoderModule(Module, AutoDoc):
    """Hardware counting of quadrature encoder signals."""
    pads_layout = [("pin_A", 1), ("pin_B", 1), ("pin_Z", 1)]

    COUNTER_SIZE = 32

    def __init__(self, encoder_config: EncoderConfig, pads=None) -> None:

        # AutoDoc implementation
        self.intro = ModuleDoc("""
        Hardware counting of quadrature encoder signals.

        Encoder is used to measure position by counting the pulses generated by a
        quadrature encoder. For each pulse it takes 3 clock-cycles for the FPGA to
        process the signal. For a 50 MHz FPGA, this would result in a roughly 15
        MHz count rate as a theoretical upper limit.

        One also should take into account that the Z-signal has to be processed by
        LinuxCNC. Given a 1 kHz servo-thread (perios 1000 ns), this would lead to an
        upper limit of 60000 RPM (1000 Hz) for the encoder. That's really fast, but
        gives with a 2500 PPR encoder a merely 2.5 MHz count-rate...

        The counters used in this application are signed 32-bit fields. This means
        that at a count-rate of 2.5 MHz (which is deemed the real practical upper
        limit). The counter will overflow in 858 seconds (just shy of 15 minutes)
        in case it is running at top speed and it is not reset.
        """)
        # Require to test working with Verilog, basically creates extra signals not
        # connected to any pads.
        if pads is None:
            pads = Record(self.pads_layout)
        self.pads = pads

        # Exported pins
        pin_A = Signal()
        pin_B = Signal()
        pin_Z = Signal()

        # Exported fields
        self.index_enable = Signal()
        self.counter = Signal((self.COUNTER_SIZE, True), reset=encoder_config.reset_value)
        self.index_pulse = Signal()
        self.reset_index_pulse = Signal()
        self.reset = Signal()

        # Internal fields
        pin_A_delayed = Signal(3)
        pin_B_delayed = Signal(3)
        pin_Z_delayed = Signal(3)  # NOTE: Z is delayed for 2 cycles (0,1) the third postion is
                                   # used to detect rising edges.
        count_ena = Signal()
        count_dir = Signal()

        # Program
        # - Create the connections to the pads
        self.comb += [
            pin_A.eq(pads.Encoder_A),
            pin_B.eq(pads.Encoder_B),
        ]
        # - Add support for Z-index if pin is defined. If not, the Signal is set to be constant
        if hasattr(pads, 'Encoder_Z'):
            self.comb += pin_Z.eq(pads.Encoder_Z)
        else:
            self.comb += pin_Z.eq(Constant(0))
        # - In most cases, the "quadX" signals are not synchronous to the FPGA clock. The
        #   classical solution is to use 2 extra D flip-flops per input to avoid introducing
        #   metastability into the counter (src: https://www.fpga4fun.com/QuadratureDecoder.html)
        self.comb += [
            count_ena.eq(pin_A_delayed[1] ^ pin_A_delayed[2] ^ pin_B_delayed[1] ^ pin_B_delayed[2]),
            count_dir.eq(pin_A_delayed[1] ^ pin_B_delayed[2])
        ]
        self.sync += [
            pin_A_delayed.eq(Cat(pin_A, pin_A_delayed[:2])),
            pin_B_delayed.eq(Cat(pin_B, pin_B_delayed[:2])),
            pin_Z_delayed.eq(Cat(pin_Z, pin_Z_delayed[:2])),
            # Storing the index pulse (detection of raising flank)
            If(
                pin_Z_delayed[1] & ~pin_Z_delayed[2],
                self.index_pulse.eq(1)
            ),
            # Reset the index pulse as soon the CPU has indicated it is read
            If(
                self.reset_index_pulse & self.index_pulse,
                self.index_pulse.eq(encoder_config.reset_value),
                self.reset_index_pulse.eq(0)
            ),
            # When the `index-enable` flag is set, detext a raising flank and
            # reset the counter in that case
            If(
                self.reset | (self.index_enable & pin_Z_delayed[1] & ~pin_Z_delayed[2]),
                self.counter.eq(encoder_config.reset_value),
                self.index_enable.eq(0)
            ),
            # Counting implementation. Counting occurs when movement occcurs, but
            # not when the counter is reset by the `index-enable`. This takes into
            # account the corner-case when the reset and the count action happen
            # at exact the same clock-cycle, which (in simulations) showed the reset
            # would not happen.
            If(
                count_ena & ~(self.index_enable & pin_Z_delayed[1] & ~pin_Z_delayed[2]),
                If(
                    count_dir,
                    self.create_counter_increase(encoder_config),
                ).Else(
                    self.create_counter_decrease(encoder_config)
                )
            )
        ]

    def create_counter_increase(self, encoder_config: EncoderConfig):
        """
        Creates the statements for increasing the counter. When a maximum
        value for the counter is defined, this is taken into account.
        """
        if encoder_config.max_value is not None:
            return If(
                self.counter < encoder_config.max_value,
                self.counter.eq(self.counter + 1),
            )
        return self.counter.eq(self.counter + 1)

    def create_counter_decrease(self, encoder_config: EncoderConfig):
        """
        Creates the statements for decreasing the counter. When a minimum
        value for the counter is defined, this is taken into account.
        """
        if encoder_config.min_value is not None:
            return If(
                self.counter > encoder_config.min_value,
                self.counter.eq(self.counter - 1),
            )
        return self.counter.eq(self.counter - 1)

    @classmethod
    def add_mmio_read_registers(cls, mmio, config: List[EncoderConfig]):
        """
        Adds the status registers to the MMIO.

        NOTE: Status registers are meant to be read by LinuxCNC and contain
        the current status of the encoder.
        """
        # Don't create the registers when the config is empty (no encoders 
        # defined in this case)
        if not config:
            return
            
        # At least 1 encoder exits, create the registers.
        mmio.encoder_index_pulse = CSRStatus(
            size=int(math.ceil(float(len(config))/32))*32,
            name='encoder_index_pulse',
            description="""Encoder index pulse
            Register containing the flags that an index pulse has been detected for the given
            encoder. After succefully reading this register, the index pulse should be reset
            by writing a 1 for the given encoder to the `reset index pulse`-register.
            """
        )
        for index in range(len(config)):
            setattr(
                mmio,
                f'encoder_{index}_counter',
                CSRStatus(
                    size=cls.COUNTER_SIZE,
                    name=f'encoder_{index}_counter',
                    description="Encoder counter\n"
                    f"Register containing the count for register {index}."
                )
            )

    @classmethod
    def add_mmio_write_registers(cls, mmio, config: List[EncoderConfig]):
        """
        Adds the storage registers to the MMIO.

        NOTE: Storage registers are meant to be written by LinuxCNC and contain
        the flags and configuration for the encoder.
        """
        # Don't create the registers when the config is empty (no encoders 
        # defined in this case)
        if not config:
            return

        # At least 1 encoder exits, create the registers.
        mmio.encoder_index_enable = CSRStorage(
            size=int(math.ceil(float(len(config))/32))*32,
            name='encoder_index_enable', 
            description="""Index enable
            Register containing the `index enable`-flags. When true, the counter of the given
            encoder is reset to zero. This field has to be set for each index-pulse generated
            by the encoder.
            """, 
            write_from_dev=True)
        mmio.encoder_reset_index_pulse = CSRStorage(
            size=int(math.ceil(float(len(config))/32))*32,
            name='encoder_reset_index_pulse',
            description="""Reset index pulse
            Register containing the detected index pulse should be cleared on the next clock
            cycle. Indicates the CPU has successfully read the index pulse from the card and
            has processed it.
            """
        )

    @classmethod
    def create_from_config(cls, soc: SoC, config: List[EncoderConfig]):
        """
        Adds the encoders as defined in the configuration to the SoC.

        NOTE: the configuration must be a list and should contain all the encoders at
        once. Otherwise naming conflicts will occur.
        """
        # Don't create the module when the config is empty (no encoders 
        # defined in this case)
        if not config:
            return
        
        # At least 1 encoder exits, create the module(s).
        # - create a list of `index_pulse`-flags. These will be added later on
        #   outside the mainloop in a Cat-statement.
        index_pulse = []
        # - main loop for creating the encoders
        for index, encoder_config in enumerate(config):
            # Add the io to the FPGA
            if encoder_config.pin_Z is not None:
                soc.platform.add_extension([
                    ("encoder", index,
                        Subsignal("Encoder_A", Pins(encoder_config.pin_A), IOStandard(encoder_config.io_standard)),
                        Subsignal("Encoder_B", Pins(encoder_config.pin_B), IOStandard(encoder_config.io_standard)),
                        Subsignal("Encoder_Z", Pins(encoder_config.pin_Z), IOStandard(encoder_config.io_standard))
                    )
                ])
            else:
                soc.platform.add_extension([
                    ("encoder", index,
                        Subsignal("Encoder_A", Pins(encoder_config.pin_A), IOStandard(encoder_config.io_standard)),
                        Subsignal("Encoder_B", Pins(encoder_config.pin_B), IOStandard(encoder_config.io_standard)),
                    )
                ])
            # Create the encoder
            pads = soc.platform.request("encoder", index)
            encoder = cls(encoder_config=encoder_config, pads=pads)
            # Add the encoder to the soc
            soc.submodules += encoder
            # Hookup the ynchronous logic for transferring the data from the CPU to FPGA
            soc.sync += [
                # Reset the counter when LinuxCNC is started
                encoder.reset.eq(soc.MMIO_inst.reset.storage),
                # `index enable`-flag
                encoder.index_enable.eq(
                    soc.MMIO_inst.encoder_index_enable.storage[index]
                ),
                # `reset index pulse`-flag (indication data has been read by CPU)
                encoder.reset_index_pulse.eq(
                    soc.MMIO_inst.encoder_reset_index_pulse.storage[index]
                )
            ]
            soc.sync += encoder.index_enable.eq(soc.MMIO_inst.encoder_index_enable.storage[index])
            # Add combination logic for getting the status of the encoders
            soc.sync += getattr(soc.MMIO_inst, f"encoder_{index}_counter").status.eq(encoder.counter)
            # Add the index pulse flag to the output (if pin_Z is defined). Last step is to Cat this
            # list to a single output
            index_pulse.append(encoder.index_pulse if encoder_config.pin_Z is not None else Constant(0))

        # Add combination logic for getting the  `index pulse`-flag. We have to use Cat here
        # so it is not possible to do this in the main loop.
        soc.comb += [
            soc.MMIO_inst.encoder_index_pulse.status.eq(Cat(index_pulse)),
        ]


if __name__ == "__main__":
    from migen import *
    from migen.fhdl import *


    def test_encoder(encoder):
        i = 0
        while(1):
            if i % 2 == 0:
                yield(encoder.pads.pin_A.eq(~encoder.pads.pin_A))
            else:
                yield(encoder.pads.pin_B.eq(~encoder.pads.pin_B))
            # Simulate the index enable method (one shot)
            if i == 0:
                yield(encoder.index_enable.eq(1))
            # Simulate a Z-signal going high when i = 100 and 200
            if i == 100 or i == 200:
                yield(encoder.pads.pin_Z.eq(1))
            else:
                yield(encoder.pads.pin_Z.eq(0))
            # Reset the Z-signal by raising the `reset_index_pulse`-flag
            if i == 110:
                yield(encoder.reset_index_pulse.eq(1))
            index_pulse = yield encoder.index_pulse
            counter = yield encoder.counter
            index_enable = yield encoder.index_enable
            print("counter, index = %d, %d @index_enable %d @clk %d"%(counter, index_pulse, index_enable, i))
            yield
            i+=1
            if i > 1000:
                break
    
    config = EncoderConfig(
        name="test",
        pin_A="not_used:A",
        pin_B="not_used:B",
        min_value=40,
        max_value=150,
        reset_value=100
    )
    encoder = EncoderModule(config)
    print("\nRunning Sim...\n")
    # print(verilog.convert(stepgen, stepgen.ios, "pre_scaler"))
    run_simulation(encoder, test_encoder(encoder))
