---
title: The Buried Engine — A Structural Test Chapter
---

# Contents {#contents}

- [I. Petropolitics and the Hole Complex](#petropolitics)
- [II. Hyperstition as a Feedback Circuit](#hyperstition-circuit)
- [III. A Toy Model of Porosity](#porosity-model)
- [Notes on Method](#method)

# I. Petropolitics and the Hole Complex {#petropolitics}

Petroleum does not rest. Beneath the Xerodrome the buried life of the planet keeps moving, a slow black current that the *Tellurian* order can neither name nor stop.[^1] To read the desert as a surface is already a mistake; the surface is only the lid of a deeper machinery, and the machinery has its own appetite. Hamid Parsani called this appetite **the Hole complex**, a term he never defined twice in the same way.

For Parsani, Petropolitics is not a branch of geopolitics but its unconscious. Every pipeline that crosses the Middle East is a small confession that the ground was never solid; every refinery is an altar where Solar Capitalism pretends to burn what it cannot digest. As we will see in [Section III](#porosity-model), the confession can be modelled, at least crudely, as a problem of flow through a porous medium.

> Oil is the undercurrent of all narrations, not only the political but also the religious and the ethical. It is the lubricant of the planetary engine.[^2]

The quotation above is usually read as a provocation. It is better read as a **technical claim** about Porosity: a solid that is riddled with holes behaves, at a sufficient scale, like a fluid. The distinction between Solid and Fluid, which organizes so much of the *War on Terror* imaginary, dissolves exactly where the Hole complex begins.

## The Grammar of Holes {#grammar-of-holes}

Parsani distinguishes three kinds of openings:

1. **Surface holes**, which only puncture the lid and leave the deeper machinery untouched;
2. **Tunnels**, which connect two openings and therefore create a *line of flight*;
3. **Plot Holes**, which are not openings in the earth at all but gaps in a narrative through which something else can enter.

The third kind is the decisive one. A Plot Hole is where Hidden Writing leaks into history, and where, according to the notes collected by Kristen Alvanson, the figure of Pazuzu first appears in Parsani's manuscripts.[^3]

![Figure 1. Parsani's sketch of the three kinds of openings, redrawn from the manuscript.](images/hole-complex.png)

# II. Hyperstition as a Feedback Circuit {#hyperstition-circuit}

Hyperstition works here like a fever. A fiction is written, the desert answers, and the answer rewrites the fiction until no one can say which came first. The CCRU described this loop with deliberate vagueness, but its structure is simple enough to state: a belief $b_t$ increases the probability of an event, and the event in turn reinforces the belief.

We can write the loop as a pair of coupled update rules:

$$
b_{t+1} = b_t + \alpha \, e_t (1 - b_t), \qquad e_{t+1} \sim \mathrm{Bernoulli}(\sigma(\beta b_{t+1}))
$$

where $\alpha \in (0, 1]$ is the *credulity* of the population and $\beta > 0$ measures how strongly belief bends events. When $\alpha\beta$ exceeds a threshold the loop runs away; below it, the fiction remains a fiction.[^4] This is not offered as a proof but as a picture: Hyperstition is a positive feedback circuit whose gain is set by culture.

The War Machine that crosses this ground is not an army but a tendency. It moves as Dust moves, without a center, refusing the striated map that the state lays over the buried life. Dustism, in this sense, is the politics of a medium that cannot be held — see also the discussion of Radical Openness in [the Grammar of Holes](#grammar-of-holes).

# III. A Toy Model of Porosity {#porosity-model}

To make the claim about solids and fluids concrete, consider a lattice in which each site is open with probability $p$. Percolation theory tells us that an infinite connected cluster appears only when $p$ exceeds a critical value $p_c$; for the square lattice $p_c \approx 0.5927$. Table 1 summarizes the regimes.

| Regime | Condition | Behaviour of the medium |
|--------|-----------|-------------------------|
| Solid | $p < p_c$ | Holes are isolated; nothing flows through |
| Critical | $p = p_c$ | Fractal clusters; flow is intermittent |
| Fluid | $p > p_c$ | A spanning cluster exists; the medium leaks |

: Table 1. Percolation regimes and their reading in terms of the Hole complex.

The simulation below estimates the probability that a cluster spans the lattice. It is deliberately naive and should not be used for anything serious.

```python
import random

def spans(n: int, p: float) -> bool:
    """Return True if an open cluster connects the top row to the bottom row."""
    grid = [[random.random() < p for _ in range(n)] for _ in range(n)]
    frontier = [(0, j) for j in range(n) if grid[0][j]]
    seen = set(frontier)
    while frontier:
        i, j = frontier.pop()
        if i == n - 1:
            return True
        for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            a, b = i + di, j + dj
            if 0 <= a < n and 0 <= b < n and grid[a][b] and (a, b) not in seen:
                seen.add((a, b))
                frontier.append((a, b))
    return False
```

Running `spans(200, p)` for $p$ between 0.55 and 0.65 reproduces the familiar S-shaped curve. What matters for our reading is not the curve itself but the fact that the transition is *sharp*: a medium does not become porous gradually. It becomes porous all at once, the way a fiction becomes true.

# Notes on Method {#method}

This chapter alternates between commentary and formalization on purpose. The formulas are not meant to explain Parsani; they are meant to show where his vocabulary can be translated into a different idiom without loss, and where it cannot. Readers who want the original context should start with *Cyclonopedia: Complicity with Anonymous Materials* (re.press, 2008) and the essays collected on the [Hyperstition blog](http://hyperstition.abstractdynamics.org/).[^5]

[^1]: On the Tellurian as a political category, see Reza Negarestani, *Cyclonopedia*, pp. 15–19. The term is used here without its later theological connotations.

[^2]: Reza Negarestani, *Cyclonopedia: Complicity with Anonymous Materials* (Melbourne: re.press, 2008), p. 19.

[^3]: The manuscripts themselves are lost. What survives are photocopies of 47 pages, some of them annotated in a hand that is not Parsani's.

[^4]: For the closely related logistic model, the threshold is $\alpha\beta = 4$; we do not claim the same value holds here.

[^5]: The blog was active between 2004 and 2007. An archived copy is available via the [Internet Archive](https://web.archive.org/).
