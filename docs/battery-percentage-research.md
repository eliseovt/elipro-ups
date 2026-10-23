# NT-2402PRO battery display profile: two manual cycles and validation limits

The versioned `nt2402pro-pmv-qs-v1` display profile was checked against this
NT-2402PRO and Forza V3.00-21021 during two manual power-cut observations. The
first supplied an eight-point discharge fixture; the second supplied a complete
74-point line, battery and recovery fixture, all reproduced exactly at the
reference evaluation cadence. This validates the observed display behavior for
these captures. It does not calibrate state of charge, measure remaining
autonomy or validate real Windows shutdown/hibernation actions.

## Current measurement contract

The native NT-2402PRO reader decodes the observed binary QS/T response. It
reports battery voltage and the real low-battery flag, but no battery percentage
or remaining runtime. `battery_percent` and `runtime_seconds` remain `None`.
The QS parser and native adapter tests cover this explicitly. The normal reader
continues to send only `QS` followed by CR.

The bridge maps ForzaTracker's `workInfo.batteryCapacity` into its existing
percentage field. That value is vendor output, not an independent measurement
of state of charge or battery health. The independent native profile described
below is a display estimate, not a calibrated state of charge measurement.

## Read-only investigation of the installed Forza application

The installation identifies itself as Forza V3.00-21021 and its configuration
contains `Customized=Forza`. That key affects the ordinary PMV processor; it
must not be assumed to select the same branch in the binary QS/T variant.
The investigation inspected Java class metadata and bytecode
inside `C:\Forza\monitor\viewpowermonitor.jar`, without running its classes,
starting the application or issuing device commands. No vendor code or binaries
were copied into this repository. The following is a behavioral summary.

`cn.com.voltronicpower.monitor.processor.PMVProcessor.queryWorkInfo()` reads
voltage from QS. At bytecode offsets 355–424 it selects a divisor from the
observed voltage: below 15 V it keeps the value; through 30, 45 and 60 V it
divides by 2, 3 and 4 respectively. This normalization does not read a battery
block count, nominal voltage or the QS ratings byte.

`PMVProcessor.calculateBatteryCapacity(float,String)` estimates a percentage
from that normalized voltage and work mode:

- On line/standby, below 11.6 V becomes 0%; at least 13.5 V becomes 100%.
  Between 11.6 and 13.3 V it uses the integer part of
  `90 * (voltage - 11.6) / 1.7`.
- Between 13.3 and 13.5 V it uses internal state initialized to 90% and raises it
  by one point after each 720000 ms interval, up to 100%.
- On battery/test, it uses a linear 0–100% interval from 10.2 to 13.2 V when UPS
  output load is below 20%, or from 10.2 to 12.7 V otherwise. This path also
  resets the charging timer and its capacity state.

The Forza customization in ordinary PMV `queryWorkInfo()` (offsets 866–1597) can replace that
estimate. It invokes the strings `BT?` and `BL?` through the communication
handler. `monitor.protocol.PMV` defines the same strings as its BT and BL
constants. This call site is part of periodic status processing; it is separate
from the vendor's battery-test and power-control methods. That static evidence
does not substitute for a device-level response validation.

- BT parsing removes the first response character and parses the remainder as
  an integer. The prefix and units have not been confirmed from a real capture.
- If BT exceeds 10800, the code assigns 0%. Otherwise it obtains BL.
- The BL parser accepts an integer or the literal `---`. The literal selects the
  voltage estimate; an integer supplies the percentage with an upper limit of
  100%.
- On line/standby the Forza path further constrains the percentage using total
  voltage bands: above 27 V → 100%; (26,27] → 75–99%; (24,26] → 50–74%;
  (23,24] → 25–49%; at most 23 V → 0–24%.
- On battery, BT below 80 together with BL above 9 starts a separate capacity
  state at no more than 30%. Subsequent evaluations decrease that state by five
  points until 14, then one point until 9. A reset of this state when mains
  returns was not established by the inspected method.

`cn.com.voltronicpower.data.bean.WorkInfo.setBatteryCapacity(int)`, found in the
installed `viewpowerRMI.jar`, applies an integer average over a ten-value
window. Several Forza branches call it twenty times to replace that window.
`setBatteryCapacityReal(int)` directly assigns the value, but the normal
`PMVProcessor` path does not use that setter.

`USBHandler` passes the command and a CR to its USB communication interface.
Its fixed BL value of 99 belongs to a test fixture; that test mode is false by
default and is not evidence of a real UPS reply. No matching QS, BT, BL or
battery-capacity observations were found in the inspected `C:\Forza\log` files.

## Binary QS/T variant and observed fixture

`PMV_SimplifyProcessor` extends `PMVProcessor` and inherits its capacity formula,
but replaces `queryWorkInfo()`. The replacement does not use BT/BL or the Forza
customization above: it calls the inherited calculation, then
`WorkInfo.setBatteryCapacity()` once per successful evaluation. Both variants
publish the protocol identifier `PMV`, so that API label does not distinguish
their implementations. The ordinary PMV matcher requires device type `V`; the
simplified matcher accepts type `P` or `T` and its serial path expects a response
beginning with `#`.

The simplified `calcBatVoltage()` multiplies the two integer hex fields, divides
by 510 using Java float arithmetic, and formats the result with `%.1f`. It then
parses that one-decimal voltage as a float before normalizing to a 12 V block.
`WorkInfo.setBatteryVoltage()` merely stores the string. The inherited capacity
formula ends in a Java double-to-int cast (truncation), not `Math.round()`.

The capacity setter initially fills its ten-value window with the first value.
After initialization, it returns the integer mean of the *previous* ten values,
then shifts in the new value. Thus there is a one-evaluation delay. With a window
already filled with 100% during line operation, the following independently
observed fixture is reproduced exactly by the simplified path. All recorded
loads were below 20%.

| Battery voltage reported by Forza | Base calculation, truncated | Predicted window result | Observed Forza result |
| --- | --- | --- | --- |
| 25.6 V | 86 | 100 | 100 |
| 25.3 V | 81 | 98 | 98 |
| 25.3 V | 81 | 96 | 96 |
| 25.3 V | 81 | 94 | 94 |
| 25.3 V | 81 | 92 | 92 |
| 25.3 V | 81 | 91 | 91 |
| 25.3 V | 81 | 89 | 89 |
| 25.2 V | 80 | 87 | 87 |

The fixture comes from technical observations retained locally in
`.test-data/battery-validation/forza-observations.jsonl`. Changes appeared roughly
every 4.08 seconds in that capture, with one approximately 5.12-second gap at the
observer's sampling resolution. `WorkMonitor.run()` sleeps 3000 ms before each
cycle, in addition to time spent querying the device; it is not an exact
four-second clock. The smoother is defined by ten evaluations, not ten seconds.
Applying it directly to a different polling cadence changes its time response.

In a separate explicit native diagnostic, QS reported Battery mode, 25.29 V and
10–11% output load, flags `89/A2`; both BT and BL replied with the bytes `N\r`.
Those replies are retained as unsupported, not decoded as zero or a percentage.
In the *ordinary* PMV Forza path, a normalized BT response `N` would make
`substring(1)` empty and integer parsing fail. Its exception handler calls
`WorkInfo.clear()`, which clears voltage/mode and sets capacity to zero. It does
not retain the locally computed base value. That path therefore cannot explain
the valid observed decreasing percentages. The simplified path explains both
the binary QS/T data and the observed smoothing without requiring BT/BL support.

### Second-cycle comparison and timestamp ownership

The second independently observed cut and recovery were retained locally in
`.test-data/battery-validation/forza-comparison.jsonl`. Its anonymized fixture is
versioned in `tests/fixtures/battery-pmv-cycle.json`, with relative `elapsed_ms`,
technical sample fields and the independently observed vendor percentage. It
contains no absolute timestamps, device identifiers or serial numbers. The voltage curve and
filter reproduced all 74 completed evaluation points after separating record
timestamps from measurement evaluations. Both discharge and recovery matched;
the model did not require a different online formula.

The vendor's `workInfo.currentTime` is not an atomic measurement sequence.
`queryWorkInfo()` sets it before completing its queries. Independently,
`RecordHandler.run()` obtains the live shared object through
`MonitorProcessor.getWorkInfo()` and sets `currentTime` to a new date before
saving it (bytecode offset 111). `AbstractMonitorProcessor.getWorkInfo()` returns
the field directly, without copying it. This periodic write changes the API
timestamp without calculating capacity or inserting anything into its window.

A replay that assumes every distinct `vendor_time_ms` represents a completed
evaluation therefore creates extra evaluations and can shift recovery by one
or two points. Taking the last row in each timestamp group alone is insufficient:
an independently changed record timestamp can split one evaluation across two
groups, even when both groups contain repeated stable rows.

For this capture, the two relevant pairs were groups 54/55 and 69/70 (zero-based
order of distinct timestamp groups). Within each pair, the first timestamp was
kept and the last group's data were used. Their first timestamps and completed
values were respectively `elapsed_ms=215659` / 26.2 V / 80%, and
`elapsed_ms=274870` / 27.3 V / 98%. The intervening record timestamps were only 1.631 and 1.965 seconds
after their respective evaluation starts, shorter than the monitor loop's
mandatory three-second sleep. The final still-incomplete group was excluded.
Feeding each resulting evaluation once, with the retained timestamp as the
model clock, gave 74/74 exact results. This reconciliation is specific evidence
for the captured sequence, not permission to treat every vendor API timestamp
as a new physical measurement.

Arithmetic was checked independently in bytecode: the online branch computes
the integer truncation of `(90.0 * (double(normalized_float32) - 11.6)) / 1.7`.
The constants and intermediate arithmetic in this formula are Java doubles.
Matching a smoothed vendor display across these short observations still does
not calibrate state of charge or establish remaining autonomy.

## Explicit observation tool

The diagnostic is opt-in and separate from monitor polling. From source:

```powershell
python -m ups_monitor.battery_diagnostics --samples 1 --interval 1 --label network
```

In the packaged executable:

```powershell
Elipro.exe battery-diagnostic --samples 1 --interval 1 --label network
```

`--samples` accepts 1–120, default 1. `--interval` is a finite number of seconds
at least 1, default 1, and waits between captures. `--label` accepts `network`,
`battery`, `recovery` or `manual` (default). Labels only describe the operator's
observation; they do not change hardware state or simulate a source.

The tool obtains `MonitorInstanceGuard` before discovery, refuses an active or
unverifiable Forza collector, and requires exactly one matching device with HID
reports 9/9/0. It does not stop a monitor or collector. Each query uses the
existing device mutex, bounded child process, deadline, cooldown and uncertain
worker quarantine. The diagnostic method permits exactly `BT?\r` and `BL?\r`;
`query_status()` retains its separate QS-only allowlist.

Stdout is one JSON object with `schema_version`, `diagnostic`,
`physical_validation: "pending"`, `estimation: "not_computed"`, and `samples`.
Each sample has a UTC `timestamp`, `capture_label` and `qs`, `bt`, `bl` records.
Each query record includes lossless `raw_hex`, escaped `raw_text`, `decoded`,
`format_status` and `error`. QS uses the existing strict parser. BT remains
undecoded because its prefix and units are unverified. BL only decodes a complete
ASCII integer 0–100, marked `validation: "format_only"`; `---` is unavailable.
The observed negative reply `N\r` is unsupported for both BT and BL and has no
decoded value; it is preserved without treating it as a transport error.
Other BL formats or out-of-range values are retained with an error and are not
clamped. A BT/BL error does not discard a successful QS. A failed QS skips the
additional queries. Exit status is 1 on startup or query errors, otherwise 0.

No store, notification channel, monitor, protection engine or OS power callback
is created, and no captured value changes `battery_percent` or protection
thresholds. The tool writes no output file itself; the operator chooses stdout
redirection when collecting evidence.

## Native estimate and validation scope

The explicit diagnostic and two manual comparison cycles above have been collected.
They validate the tested profile's agreement with the vendor display as a
voltage-derived, smoothed estimate;
they do not establish battery health, remaining autonomy or a calibrated state
of charge. The diagnostic remains observational and computes no final estimate.

The original `nt2402pro-pmv-qs-v1` implementation uses only ordinary QS readings
for this observed 24 V device profile. It quantizes voltage to one decimal,
normalizes using float32 and applies the PMV curve and a previous-window integer
average over ten evaluations. A cold start seeds the window from the first
valid curve value rather than assuming a full battery. It retains history when
mains returns and resets on invalid modes, failed reads, OEM contention or an
unobserved gap over 15 seconds. Evaluations are separated by at least three
seconds; at the native two-second polling rate this normally produces a
four-second filter update. Exact display timestamps can differ from Forza's
three-second sleep plus its communication delays.

The fields `battery_percent_estimate` and `battery_estimate_method` retain their
own provenance and never replace `battery_percent` or the real low-battery flag.
Since 0.9.17, an explicitly labelled, user-authorized low-percentage protection
may use this known estimate to shorten an enabled Windows countdown to at most
10 seconds. Reported percentages take precedence. Unknown estimate methods do
not trigger the priority. The profile calculates no remaining runtime and
performs no additional USB transaction. Legacy samples
are never retroactively estimated. CSV exports append both fields after all
existing columns, including `watts_is_estimate`.

The native polling path remains QS-only. The observed device rejects BT/BL in
the separately invoked diagnostic. Any future attempt to interpret other replies
still needs its actual framing, BT prefix and units established. Compare independently
labeled network, battery and recovery observations. Check restart/state effects
and distinguish a vendor-reported value from the voltage-derived heuristic.
The tests use mocks only and do not certify these hardware properties.

Any future inferred percentage must retain its own provenance and validation
scope. It must not silently populate the existing `battery_percent` field: that
field participates in automatic protection and low-battery alerts. Neither these
formulas nor absence of the low-battery flag proves remaining autonomy, battery
health or a fully charged battery.
