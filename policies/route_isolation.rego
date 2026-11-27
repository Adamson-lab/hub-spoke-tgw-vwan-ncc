package hubspoke.isolation

import rego.v1

default allow := false
default reason := "denied: spoke-to-spoke routing violates hub isolation rule"

# Spokes in separate hubs cannot route directly unless explicitly bridged through a shared zone.
allow if {
	input.source_hub == input.target_hub
	input.source_zone != "sandbox"
}

allow if {
	input.source_hub != input.target_hub
	input.cross_hub_bridge == true
	input.source_zone == "shared"
}

reason := "allowed: intra-hub spoke route approved" if {
	allow
	input.source_hub == input.target_hub
}

reason := "allowed: bridged via shared transit hub" if {
	allow
	input.source_hub != input.target_hub
}
