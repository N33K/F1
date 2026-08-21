Issue: There is small amount of understanding what is MJ in a context of the energy recovery system used in new formula 1 cars. This issue creates confusion among the fans, and a lot of other problems for the analytics of the results.

Solution: Create a simulation engine, which will use real F1 data to calculate energy usage and regeneration

How it works:
1. The data about the weekend is taken from the available sessions, if any exist in this season, if not, information is taken from previous seasons. Data received with FastF1 API, which is an official data from F1, fetched Distance, Speed, Throttle, Brake, RPM, nGear used for next calculations
2. Fetched data then used in the physical formulas, which also using constant values for downforce model, which is used on a certain track, weight of the car (separated between Race (mid session weight of the car + fuel load) and Qualification (weight of the car + low fuel load for 1-2 laps)), SOC of the battery. Formulas negate inclination, relative sea level height, and air humidity to not overcomplicate model.
   Model should correctly calculate what happens with the battery on three different events:
	1. Driver applies full throttle - Battery being discharged on the rate, explained below;
	2. Driver applies brakes - Battery being charged on the rate, explained below;
	3. Driver uses lift and coast technique - where no throttle/braking inputs applied, and car regenerates energy from the deceleration of the crankshaft. While no inputs are applied, all aero elements are also switch into closed position, thus increasing drag.
3. Fetched data then passed to the frontend part and visualized.


Explanation of the battery usage on the cars of 2026 technical regulation:
Each car have 4mj battery capacity. This limit is mandated by the FIA and cannot be changed;
Driver can also harvest limited amount of energy, this number is defined for each track and session before the race, also provided by the FIA (we are fetching this data straight from the FIA site), this number can vary between 7 and 8.5mj on most of the tracks.
Electrical engine provides 350kw of the power, with fixed energy decline over time. Each second, energy drops by 50kw, which correlates to roughly 8 seconds of the electrical power used assuming, that driver goes this distance applying full throttle.
Cars also can use superclipping - system, which uses special module in the powertrain of the f1 car, which is connected to the crankshaft of the engine, and regenerates energy from the kinetic energy of the spinning crankshaft. Maximum amount of the energy which can be recovered this way also limited by the FIA, and limits to 250kw per second of restored energy, which correlates to 2MJ of energy restored over 8 seconds from previous example

Here is table with breakdown of how car uses energy through a straight with such parameters:
fully charged battery (4MJ of available energy) Superclipping is happening Ramp-down accounted for Full throttle applied:
### The Calculation:

| Second | Deployment (kW) | Recovery (kW) | Net Flow (kJ) | Battery State (kJ) |
| ------ | --------------- | ------------- | ------------- | ------------------ |
| 1      | 350             | 250           | -100          | 3,900              |
| 2      | 300             | 250           | -50           | 3,850              |
| 3      | 250             | 250           | 0             | 3,850              |
| 4      | 200             | 250           | +50           | 3,900              |
| 5      | 150             | 250           | +100          | 4,000              |
| 6      | 100             | 250           | +150          | 4,000*             |
| 7      | 50              | 250           | +200          | 4,000*             |
| 8      | 0               | 250           | +250          | 4,000*             |
Cars also can regenerate energy on braking, with this rate:
- **Maximum recovery rate during braking:** 350kW (same as deployment limit)
- **Total per lap:** 8-9 MJ across all braking zones combined
This means, that per one second of braking, car restores 350KJ of energy. Practically, this means, that longer braking = more energy harvested

Possible limitations: 
1. FastF1 library doesn't provide granular braking force, it only shows if brakes are applied in boolean format (true/false). To overcome this limitation, we can calculate braking force ourself, using speed delta overtime:
   **Speed delta over time:**

- Change in speed = indication of braking intensity
- Larger delta = harder braking
- `braking_intensity ≈ (speed_before - speed_after) / braking_duration`