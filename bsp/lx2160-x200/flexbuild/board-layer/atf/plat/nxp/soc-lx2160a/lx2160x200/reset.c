/* SPDX-License-Identifier: BSD-3-Clause */
/* X200 authority: x200-bl31-system-reset-cpld-request and
 * x200-reset-cpld-frame-design in hardware-evidence-v1.json.
 */
#include <stdint.h>
#include <stdio.h>

#include <arch_helpers.h>
#include <drivers/console.h>
#include <drivers/delay_timer.h>
#include <lib/mmio.h>
#include <soc.h>

#define X200_DSPI2_BASE       0x02120000U
#define DSPI_MCR             0x00U
#define DSPI_CTAR0           0x0cU
#define DSPI_SR              0x2cU
#define DSPI_RSER            0x30U
#define DSPI_PUSHR           0x34U
#define DSPI_POPR            0x38U
#define DSPI_MCR_MASTER      0x80ff0000U
#define DSPI_MCR_CLEAR_FIFO  0x00000c00U
#define DSPI_MCR_HALT        0x00000001U
#define DSPI_CTAR_RESET      0x3e000014U
#define DSPI_PCS0            0x00010000U
#define DSPI_CONT            0x80000000U
#define DSPI_SR_ERRORS       0x08080000U /* TX underflow / RX overflow */
#define X200_SPI_POLL_US     1000U

extern void _soc_sys_reset(void) __attribute__((noreturn));

static uint32_t dspi_read(uint32_t reg)
{
	/* Factory ldr/str w accesses prove little-endian register access. */
	return mmio_read_32(X200_DSPI2_BASE + reg);
}

static void dspi_write(uint32_t reg, uint32_t value)
{
	mmio_write_32(X200_DSPI2_BASE + reg, value);
}

static int wait_fifo(int receive)
{
	unsigned int poll;

	for (poll = 0U; poll < X200_SPI_POLL_US; ++poll) {
		uint32_t status = dspi_read(DSPI_SR);

		if ((status & DSPI_SR_ERRORS) != 0U)
			return -3;
		if (receive ? ((status & 0xf0U) != 0U) :
			      ((status & 0xf000U) < 0x4000U))
			return 0;
		udelay(1U);
	}
	return receive ? -2 : -1;
}

static int x200_reset_request(void)
{
	/* The third data byte is defined by the BSP reset contract. */
	static const uint8_t frame[] = {0x20, 0x00, 0x4c, 0x00, 0x99, 0xff};
	unsigned int i;
	int error;

	/* Own the controller at terminal SYSTEM_RESET, regardless of OS state.
	 * Halt before programming CTAR, clear both FIFOs, then start.  CTAR is
	 * board mode 3, 8 bits, MSB first; 150 MHz / 32 = 4.6875 MHz.
	 */
	dspi_write(DSPI_MCR, DSPI_MCR_MASTER | DSPI_MCR_CLEAR_FIFO | DSPI_MCR_HALT);
	dspi_write(DSPI_RSER, 0U); /* disable inherited IRQ and DMA requests */
	dspi_write(DSPI_CTAR0, DSPI_CTAR_RESET);
	/* Generic DSPI W1C: discard stale completion/error flags, not counts. */
	dspi_write(DSPI_SR, 0x9a0a0000U);
	dspi_write(DSPI_MCR, DSPI_MCR_MASTER | DSPI_MCR_CLEAR_FIFO);

	for (i = 0U; i < sizeof(frame); ++i) {
		uint32_t command = DSPI_PCS0 | frame[i];

		error = wait_fifo(0);
		if (error != 0)
			goto failed;
		if (i + 1U < sizeof(frame))
			command |= DSPI_CONT;
		dspi_write(DSPI_PUSHR, command);
		error = wait_fifo(1);
		if (error != 0)
			goto failed;
		(void)dspi_read(DSPI_POPR);
	}
	return 0;

failed:
	/* Stop queued work without inventing an extra CPLD transaction. */
	dspi_write(DSPI_MCR, DSPI_MCR_MASTER | DSPI_MCR_CLEAR_FIFO | DSPI_MCR_HALT);
	return error;
}

#ifdef X200_RESET_TRACE
void x200_reset_marker(unsigned int after)
{
	printf("X200_RESET EL3 %s RSTCNTL=%08x\n",
	       after ? "after-write" : "entry",
	       mmio_read_32(NXP_RST_ADDR + RSTCNTL_OFFSET));
	console_flush();
}
#endif

void x200_system_reset(void)
{
	int error = x200_reset_request();

	if (error != 0) {
		printf("X200_RESET FAILED: CPLD transfer error=%d; SoC reset withheld\n", error);
		console_flush();
		for (;;)
			wfi();
	}
	/* Both Linux and U-Boot PSCI reach this common EL3 gate. */
	printf("X200_RESET CPLD request complete\n");
	console_flush();
	_soc_sys_reset();
}
