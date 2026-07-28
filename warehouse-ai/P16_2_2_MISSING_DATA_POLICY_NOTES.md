# P16.2.2 Missing Data and Policy Reporting

## Behavior

- Missing values are never guessed.
- A registered item with zero current stock is returned as `NO_AVAILABLE_STOCK` evidence rather than being hidden.
- A requested item that does not exist in inventory master, lots, inbound orders, or outbound orders is reported as unregistered.
- `earliest_full_fulfillment_at` requires an identifiable positive inventory event.
- Robot status reports each robot's status, node, and battery.
- Minimum battery output distinguishes an explicit environment setting from the system default.
- An empty inventory item filter means all registered items; it is no longer narrowed to open-work items only.
