# vmctl project rules

- With a NetBox key configured, NetBox is the source of truth for VM definitions and desired state.
- In that managed mode, every VM create or state-changing command must write NetBox before changing local libvirt state. If the NetBox write fails, do not run the local mutation.
- Without a NetBox key, commands are local only and must never read or write NetBox. NetBox audit, adoption and sync require a key.
- After a VM is recorded in NetBox, read its standard VM fields and `local_context_data.vmctl` to construct or reconcile the local VM. TOML input is a request, not an authoritative copy.
- A local failure must not delete or silently roll back the NetBox record. Report the divergence and keep a retry path.
- Keep tests that verify NetBox writes precede local mutations and that NetBox failures stop them.
- Adoption requires an existing NetBox device assigned to a cluster. It creates missing VM records only and never changes local VMs.
