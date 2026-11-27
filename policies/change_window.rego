package hubspoke.window

import rego.v1

default allow := false
default reason := "denied: route update outside UTC change window without lock extension"

# Active maintenance window lock extension permits updates for up to 15 minutes during incident recovery.
lock_extended if {
	input.window_extension.enabled == true
	count(input.window_extension.change_id) >= 4
	input.window_extension.extra_minutes <= 15
	input.actor_role in {"net-eng", "sre-oncall"}
}

allow if {
	lock_extended
}

allow if {
	not lock_extended
	input.actor_role in {"net-eng", "sre-oncall", "net-bot"}
	input.utc_hour >= 2
	input.utc_hour <= 22
}

reason := "allowed via active 15m change window extension" if {
	allow
	lock_extended
}

reason := "allowed within UTC network change window" if {
	allow
	not lock_extended
}
