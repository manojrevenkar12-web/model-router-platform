# 3. Route on the lower confidence bound of measured quality

**Decision.** A model qualifies for a task only if the lower end of the 95% interval of its measured
pass rate meets the target; cheap-first cascades only where a check makes the accepted answers meet
the target too.

**Why.** Point estimates from small samples promote lucky models: with 30 items per cell they put two
tasks clearly below target. The bound costs money when evidence is thin (more calibration lowers it),
which is the right incentive.
