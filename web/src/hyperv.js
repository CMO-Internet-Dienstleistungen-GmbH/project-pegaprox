        // ═══════════════════════════════════════════════
        // PegaProx — Hyper-V migration source (fork patch, issue #15)
        //
        // A Hyper-V host is a restricted source: PegaProx reads it, prepares a VM on it,
        // and moves that VM to Proxmox. It is never managed from here, so this file adds
        // no create, no delete and no hardware editor.
        //
        // What is left here is the preflight report and the form that registers a host.
        // Everything else a migration needs is the product's own: the source is reached
        // through the cross-hypervisor wizard, the run appears in its migration list, and
        // the VM is shown by the same panels that show an ESXi VM. See docs/adr/0001 for
        // why this file used to be four times its size.
        // ═══════════════════════════════════════════════

        // How a cluster row names its hypervisor. The list endpoint spells the field
        // `type` in some responses and `cluster_type` in others, so both are read.
        function hvType(cluster) {
            const t = cluster?.type || cluster?.cluster_type || 'proxmox';
            return ['hyperv', 'xcpng', 'esxi'].includes(t) ? t : 'proxmox';
        }

        const HV_LABELS = { hyperv: 'Hyper-V', xcpng: 'XCP-ng', esxi: 'ESXi', proxmox: 'Proxmox' };

        function hvLabel(type) {
            return HV_LABELS[type] || HV_LABELS.proxmox;
        }

        // Only a Proxmox source needs one: its VMs live on a named node. A Hyper-V host, an
        // XCP-ng pool and an ESXi host each answer for themselves.
        function hvNeedsSourceNode(cluster) {
            return !!cluster && hvType(cluster) === 'proxmox';
        }

        // Whether the sidebar entry for cross-hypervisor migration is worth showing: it
        // needs at least two clusters of different kinds, whichever kinds those are.
        function hvHasMigrationPair(clusters) {
            const kinds = new Set((clusters || []).map(hvType));
            return kinds.size > 1;
        }

        // The migration page's subtitle names the hypervisors it can actually move between.
        // Upstream's localized string predates this patch and says "Proxmox and XCP-ng",
        // which is wrong as soon as a Hyper-V host is registered. Rather than rewriting
        // nine translations for a sentence that was already incomplete — ESXi is not in it
        // either — the line is derived from what is registered, and the localized string
        // stays in use on every installation that has no Hyper-V source.
        function hvMigrationSubtitle(clusters, localized, t) {
            const kinds = new Set((clusters || []).map(hvType));
            if (!kinds.has('hyperv')) return localized;
            const named = ['proxmox', 'xcpng', 'esxi', 'hyperv']
                .filter(kind => kinds.has(kind)).map(hvLabel);
            const say = hvTranslator(t);
            // Only a source and no target yet. Upstream's sentence names Proxmox and XCP-ng,
            // which is then simply wrong about what is registered here.
            if (named.length < 2) {
                return say('hvNeedsATarget', 'Register a Proxmox cluster to migrate these VMs to');
            }
            const last = named.pop();
            return `${say('hvMigrateBetween', 'Migrate VMs between')} ${named.join(', ')} ${say('and', 'and')} ${last}`;
        }

        const HV_DIRECTIONS = {
            hyperv_to_pve: { arrow: '→ Proxmox', badge: 'HV→PVE', targetsProxmox: true },
            xcpng_to_pve: { arrow: '→ Proxmox', badge: 'XCP→PVE', targetsProxmox: true },
            esxi_to_pve: { arrow: '→ Proxmox', badge: 'ESXi→PVE', targetsProxmox: true },
            pve_to_xcpng: { arrow: '→ XCP-ng', badge: 'PVE→XCP', targetsProxmox: false },
            esxi_to_xcpng: { arrow: '→ XCP-ng', badge: 'ESXi→XCP', targetsProxmox: false },
        };

        function hvDirection(direction) {
            return HV_DIRECTIONS[direction] || { arrow: '→ Proxmox', badge: direction || '?', targetsProxmox: true };
        }

        // Whether the plan's target pickers are the Proxmox pair (node, then storage).
        function hvTargetsProxmox(direction) {
            return hvDirection(direction).targetsProxmox;
        }

        function hvIsHyperVPlan(plan) {
            return plan?.direction === 'hyperv_to_pve';
        }

        /**
         * What tells two adapters on the same virtual switch apart.
         *
         * The switch name does not: Hyper-V lets any number of adapters hang on one, and
         * on this estate that is the normal case. The MAC does, and it is also the key the
         * network map is written under — so the row now shows the thing the mapping is
         * actually made against. An adapter Hyper-V has never started reports all zeroes;
         * that is not an identity, so the position is used instead.
         */
        // The MAC of one adapter, colon-separated, or '' when the source has not assigned
        // one yet. Hyper-V reports all zeroes until a VM with a dynamic MAC has started
        // once, and zeroes are the absence of an address rather than an address.
        function hvAdapterMac(adapter) {
            const mac = (adapter?.mac_address || '').replace(/[^0-9a-fA-F]/g, '');
            if (mac.length !== 12 || /^0+$/.test(mac)) return '';
            return (mac.match(/.{2}/g) || []).join(':').toLowerCase();
        }

        // How one adapter is labelled in the wizard. The position comes first and always,
        // because everything else can be identical between two adapters on the same VM:
        // they hang on the same switch, they are both called "Network Adapter" in whatever
        // language the host speaks, and neither has a MAC until the VM has run once. Two
        // rows that read the same are two rows nobody can map correctly.
        function hvAdapterLabel(adapter, index, t) {
            const mac = hvAdapterMac(adapter);
            const name = adapter?.name && adapter.name !== 'Network Adapter' ? adapter.name : '';
            const detail = mac || (t ? (t('hvNoMacYet') || 'no MAC yet') : 'no MAC yet');
            return `#${index + 1} · ${name ? name + ' · ' : ''}${detail}`;
        }

        // The key a network map entry is stored under. It has to match what the backend
        // computes in `hyperv_preflight.adapter_key`, or the wizard writes under one key
        // and the preflight reads another — which looks like every adapter being unmapped
        // and blocks the migration with the form correctly filled in.
        function hvAdapterKey(adapter, index) {
            return adapter?.network || `adapter${index + 1}`;
        }

        function hvBytesToGiB(bytes) {
            const n = Number(bytes);
            return !n || !isFinite(n) ? '0' : (n / (1024 ** 3)).toFixed(1);
        }

        // Would Proxmox accept this as a VM name? It validates the field as a DNS name,
        // and reports the refusal from the create call — which runs after the disks have
        // been converted. Mirrors `hyperv_preflight.is_valid_pve_name` so the wizard says
        // the same thing the server would, before a 100 GiB copy is spent on finding out.
        function hvIsPveName(name) {
            const text = String(name || '').trim();
            if (!text || text.length > 255) return false;
            return text.split('.').every(label =>
                label.length <= 63 && /^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$/.test(label));
        }

        // What Proxmox accepts as `ostype`, with the Windows entries spelled out. It is a
        // field rather than a default because it decides which timers and devices the
        // guest is given: a Windows guest left on 'other' runs measurably worse with
        // nothing about it looking wrong.
        const HV_OSTYPES = [
            { value: 'other', label: 'Other / unknown' },
            { value: 'win11', label: 'Windows 11 / Server 2022+' },
            { value: 'win10', label: 'Windows 10 / Server 2016-2019' },
            { value: 'win8', label: 'Windows 8 / Server 2012 / 2012 R2' },
            { value: 'win7', label: 'Windows 7 / Server 2008 R2' },
            { value: 'l26', label: 'Linux (kernel 2.6 - 6.x)' },
            { value: 'solaris', label: 'Solaris' },
        ];

        //: The SCSI controller models Proxmox offers. `virtio-scsi-single` is what the
        //: import suggests; an older guest may need a model it has a driver for.
        //: The CPU models the import offers, mirrored from `hyperv_cpu.CPU_TYPES`. The
        //: wizard's empty entry is the node's suggestion: x86-64-v3, or x86-64-v2-AES on
        //: a processor without v3.
        const HV_CPU_TYPES = ['x86-64-v2-AES', 'x86-64-v3', 'x86-64-v4', 'host', 'kvm64', 'qemu64'];

        const HV_SCSIHW = ['virtio-scsi-single', 'virtio-scsi-pci', 'lsi', 'lsi53c810',
                           'megasas', 'pvscsi'];

        // The virtio-win release a guest may be driven by, mirrored from
        // `hyperv_drivers` so the wizard preselects what the server would accept.
        //
        // Windows Server 2012 R2 is build 9600 and takes 0.1.189 and nothing else: later
        // releases are signed in a way it does not accept, and the driver is then simply
        // not loaded — the VM stops at 0xc0000428. The rule runs the other way too:
        // 0.1.189 has no 2k22, w11 or 2k25 directory at all, so a current guest given it
        // ends up with no storage driver.
        const HV_LEGACY_BUILD = 9600;
        const HV_LEGACY_RELEASE = '0.1.189';

        function hvReleaseKey(release) {
            return String(release || '').split('.').map(n => parseInt(n, 10) || 0);
        }

        function hvNewerRelease(a, b) {
            const ka = hvReleaseKey(a), kb = hvReleaseKey(b);
            for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
                if ((ka[i] || 0) !== (kb[i] || 0)) return (ka[i] || 0) > (kb[i] || 0) ? a : b;
            }
            return a;
        }

        // Which ISO already on the node fits this guest, or null when none does.
        function hvIsoForBuild(build, isos) {
            const usable = (isos || []).filter(i => i.release);
            if (!build) return null;
            if (Number(build) === HV_LEGACY_BUILD) {
                return usable.find(i => i.release === HV_LEGACY_RELEASE) || null;
            }
            const modern = usable.filter(i => i.release !== HV_LEGACY_RELEASE);
            if (!modern.length) return null;
            return modern.reduce((best, i) =>
                hvNewerRelease(i.release, best.release) === i.release ? i : best);
        }

        // And which release should be offered for download when nothing fits.
        function hvReleaseToFetch(build, releases) {
            if (!build) return null;
            if (Number(build) === HV_LEGACY_BUILD) return HV_LEGACY_RELEASE;
            const modern = (releases || []).map(r => r.release)
                .filter(r => r && r !== HV_LEGACY_RELEASE);
            if (!modern.length) return null;
            return modern.reduce((best, r) => hvNewerRelease(r, best) === r ? r : best);
        }

        // The guest's Windows build, read off the disk that carries it.
        function hvGuestBuild(plan) {
            const disk = (plan?.guest_images || []).find(i => i.windows);
            return disk ? disk.build : null;
        }

        // What the guest calls itself. The registry knows the product name and the exact
        // patch level — "Windows Server 2022 Standard, build 20348.2582" — while the
        // image header only has a version number, and on one measured disk an older one
        // at that. So the registry answers where it could be opened, and the version
        // number is what is left when it could not.
        function hvGuestVersion(plan) {
            const volume = ((plan?.disk_inspection?.disks || [])
                .flatMap(d => d.volumes || [])
                .find(v => v.windows && v.hive_readable && v.product_name));
            if (volume) {
                const revision = volume.revision ? `.${volume.revision}` : '';
                return `${volume.product_name} (Build ${volume.build}${revision})`;
            }
            const disk = (plan?.guest_images || []).find(i => i.windows);
            return disk ? `Windows ${disk.version}` : '';
        }

        // ───────────────────────────────────────────────
        // Preflight
        // ───────────────────────────────────────────────

        const HV_SEVERITY_STYLE = {
            blocking: { dot: 'bg-red-400', text: 'text-red-400', box: 'border-red-500/30 bg-red-500/5' },
            warning: { dot: 'bg-amber-400', text: 'text-amber-400', box: 'border-yellow-500/30 bg-yellow-500/10' },
            ok: { dot: 'bg-green-400', text: 'text-green-400', box: 'border-proxmox-border bg-proxmox-dark/40' },
        };

        // Reading order: what stops the migration first, then what has to be confirmed,
        // then what is fine. Somebody preparing a VM works down that list, and burying a
        // blocker under twelve green rows is how a wizard wastes an afternoon.
        const HV_SEVERITY_ORDER = { blocking: 0, warning: 1, ok: 2 };

        /**
         * The preflight verdict, and the confirmations the caller still owes.
         *
         * A warning here is not decoration. The API refuses to start a migration whose
         * acknowledgeable warnings have not been confirmed, so these checkboxes are the
         * only way past them — and each one is a risk somebody is accepting by name.
         */
        // A translator that falls back to the English text written at the call site, so a
        // language without these keys shows a sentence rather than a key name. PegaProx
        // does the same thing product-wide with `t('x') || 'fallback'`; this only spares
        // every call site the repetition.
        function hvTranslator(t) {
            return (key, fallback) => (typeof t === 'function' && t(key)) || fallback;
        }

        function HyperVPreflight({ report, busy, acknowledged, onToggle, t }) {
            const say = hvTranslator(t);
            // The list is re-asked whenever a target choice changes, and two of its checks
            // are answered by the target rather than by the source. Saying so beats leaving
            // the previous verdict on screen as though it still applied to what is selected.
            if (busy && !report) {
                return (
                    <div className="space-y-2">
                        <div className="text-xs font-semibold text-gray-400">{say('hvPreflight', 'Preflight')}</div>
                        <div className="flex items-center gap-2 rounded-lg border border-proxmox-border
                                        bg-proxmox-dark p-3 text-xs text-gray-400">
                            <span className="w-3 h-3 rounded-full border-2 border-gray-500
                                             border-t-transparent animate-spin" />
                            {say('hvPreflightChecking', 'Checking the source and the chosen target...')}
                        </div>
                    </div>
                );
            }
            if (!report || !report.findings) return null;

            const findings = [...report.findings].sort(
                (a, b) => (HV_SEVERITY_ORDER[a.severity] ?? 3) - (HV_SEVERITY_ORDER[b.severity] ?? 3)
            );
            const needed = report.requires_acknowledgement || [];
            const confirmed = acknowledged || [];
            const outstanding = needed.filter(c => !confirmed.includes(c));

            return (
                <div className="space-y-2">
                    <div className="flex items-center justify-between">
                        <div className="text-xs font-semibold text-gray-400">{say('hvPreflight', 'Preflight')}</div>
                        {busy ? (
                            <span className="flex items-center gap-1.5 text-[10px] text-gray-400">
                                <span className="w-2.5 h-2.5 rounded-full border-2 border-gray-500
                                                 border-t-transparent animate-spin" />
                                {say('hvPreflightRechecking', 'Rechecking')}
                            </span>
                        ) : (
                            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                                report.blocked ? 'bg-red-500/20 text-red-400'
                                    : outstanding.length ? 'bg-amber-500/20 text-amber-400'
                                        : 'bg-green-500/20 text-green-400'}`}>
                                {report.blocked ? say('hvPreflightBlocked', 'Blocked')
                                    : outstanding.length
                                        ? `${outstanding.length} ${say('hvPreflightToConfirm', 'to confirm')}`
                                        : say('hvPreflightReady', 'Ready')}
                            </span>
                        )}
                    </div>

                    <div className={`space-y-1.5 max-h-64 overflow-y-auto pr-1 transition-opacity ${
                        busy ? 'opacity-50' : ''}`}>
                        {findings.map((f, i) => {
                            const style = HV_SEVERITY_STYLE[f.severity] || HV_SEVERITY_STYLE.ok;
                            const needsConfirming = needed.includes(f.check);
                            return (
                                <div key={`${f.check}-${i}`} className={`rounded-lg border p-2 ${style.box}`}>
                                    <div className="flex items-start gap-2">
                                        <div className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${style.dot}`} />
                                        <div className="min-w-0 flex-1">
                                            <div className={`text-xs font-medium ${style.text}`}>{f.summary}</div>
                                            {f.detail && <div className="text-[11px] text-gray-500 mt-0.5">{f.detail}</div>}
                                            {needsConfirming && (
                                                <label className="flex items-center gap-1.5 mt-1.5 text-[11px] text-gray-400 cursor-pointer">
                                                    <input
                                                        type="checkbox"
                                                        checked={confirmed.includes(f.check)}
                                                        onChange={() => onToggle(f.check)}
                                                        className="rounded border-gray-600"
                                                    />
                                                    {say('hvAcceptRisk', 'I accept this risk')}
                                                </label>
                                            )}
                                        </div>
                                        <span className="text-[10px] text-gray-600 shrink-0">{f.check}</span>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                </div>
            );
        }

        /**
         * Ask the preflight again, now that the target choices have been made.
         *
         * The plan is computed before the operator has picked anything on the target side,
         * so two of its checks can only answer "unknown" and block: whether the disks fit
         * needs a storage, and where each adapter lands needs a mapping. Re-asking is not
         * an optimisation — without it the wizard shows a blocker that nothing inside it
         * can clear, and the migration can never be started from the UI.
         *
         * Returns null when there is nothing to re-ask or the call fails, and the caller
         * keeps showing the plan's own verdict. A stale blocker is the safe direction to
         * fail in: the API runs the same checks again before it starts anything.
         */
        async function hvRefreshPreflight({ apiUrl, authFetch, form, plan }) {
            if (!hvIsHyperVPlan(plan) || !form?.source_cluster || !form?.source_vmid) return null;
            try {
                const resp = await authFetch(
                    `${apiUrl}/hyperv/${form.source_cluster}/vms/${form.source_vmid}/preflight`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            target_cluster: form.target_cluster || '',
                            target_node: form.target_node || '',
                            target_storage: form.target_storage || '',
                            network_map: form.network_map || {},
                            // The VLAN per adapter, so the report answers for the networks
                            // the migration would actually build rather than for the
                            // source's own values.
                            vlan_map: form.vlan_map || {},
                            // The name the VM would be created under. Without it the route
                            // checks the source's name and keeps warning that it will be
                            // renamed, however the operator has filled the field in.
                            target_name: form.target_name || '',
                            // And the driver ISO, because the release rule is checked here
                            // — where it is still free — rather than only on the node.
                            virtio_iso: form.virtio_iso_path || '',
                            // The hardware the migration would build. Without it the route
                            // falls back to its default and the driver check answers for a
                            // machine the wizard is not about to create, so the list can
                            // say "sata controller" while the box above it says VirtIO.
                            hardware: form.hardware || '',
                            // Whether the copy is started at the end, so the warning about
                            // the original's hostname and MAC appears exactly when it applies.
                            start_after: form.start_after !== false,
                        }),
                    });
                if (!resp?.ok) return null;
                return await resp.json();
            } catch (e) {
                return null;
            }
        }

        /**
         * Whether the wizard's start button may be enabled for a Hyper-V plan.
         *
         * It repeats a decision the API makes again on its own. That duplication is
         * deliberate: the button is a courtesy, the API is the gate, and a UI that could
         * be talked into enabling it must not be able to start anything.
         */
        function hvMayStart(plan, acknowledged, refreshed) {
            // The refreshed verdict wins when there is one: it was asked with the target
            // choices in hand, and the plan's was not.
            const report = refreshed || plan?.preflight;
            if (!report) return true;
            if (report.blocked) return false;
            const confirmed = acknowledged || [];
            return (report.requires_acknowledgement || []).every(c => confirmed.includes(c));
        }

        // ───────────────────────────────────────────────
        // What a Hyper-V source brings that the others do not
        // ───────────────────────────────────────────────

        function HyperVPlanFacts({ source }) {
            if (!source) return null;
            const facts = [
                ['Generation', source.generation ?? 'unknown'],
                ['Firmware', source.bios ? source.bios.toUpperCase() : 'unknown'],
                ['Machine', source.machine || 'unknown'],
                ['Checkpoints', source.checkpoint_count ?? 'unknown'],
                ['Secure Boot', hvTriState(source.secure_boot_enabled)],
                ['vTPM', hvTriState(source.vtpm_enabled)],
            ];
            return (
                <div className="grid grid-cols-3 gap-2 text-center">
                    {facts.map(([label, value]) => (
                        <div key={label} className="bg-proxmox-dark rounded-lg p-2">
                            <div className="text-xs font-bold text-white truncate">{value}</div>
                            <div className="text-[10px] text-gray-500">{label}</div>
                        </div>
                    ))}
                </div>
            );
        }

        // Three states, not two. A property the host did not report must read as unknown:
        // showing "No" for an absent answer is how a VM with a vTPM gets migrated as one
        // without.
        function hvTriState(value) {
            if (value === true) return 'Yes';
            if (value === false) return 'No';
            return 'unknown';
        }

        // ───────────────────────────────────────────────
        // Whether a target node can read this host at all
        // ───────────────────────────────────────────────

        /**
         * The host-wide SMB check, and the button that runs it.
         *
         * Whether cifs-utils is installed on the node, whether TCP 445 is open and whether
         * the account may read the share is one answer for every guest on the host. The
         * preflight used to put that question in front of every VM and ask somebody to
         * confirm an answer it did not have; 159 guests meant 159 confirmations of one
         * thing. Measured once here instead, with the date it was measured on.
         */
        function HyperVTransferCheck({ check, targets, busy, onRun, onNeedNodes, t }) {
            const say = hvTranslator(t);
            const [cluster, setCluster] = React.useState('');
            const [node, setNode] = React.useState('');

            const nodes = (targets || []).find(c => c.id === cluster)?.nodes || [];
            // A cluster nobody opened this session has no node list yet, and the picker
            // would sit empty with the button dead and nothing saying why.
            React.useEffect(() => {
                if (cluster && nodes.length === 0 && onNeedNodes) onNeedNodes(cluster);
            }, [cluster, nodes.length]);
            const ok = check && check.ok;
            const measured = check && check.at_text;

            const box = ok ? 'border-green-500/30 bg-green-500/5'
                : measured ? 'border-red-500/30 bg-red-500/5'
                    : 'border-proxmox-border bg-proxmox-dark/40';

            return (
                <div className={`rounded-lg border p-3 space-y-2 ${box}`}>
                    <div className="flex items-center justify-between gap-3">
                        <div className="text-xs font-semibold text-gray-300">
                            {say('hvTransferCheck', 'Disk access from a target node')}
                        </div>
                        {measured && (
                            <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                                ok ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'}`}>
                                {ok ? say('hvTransferCheckOk', 'reachable')
                                    : say('hvTransferCheckFailed', 'not reachable')}
                            </span>
                        )}
                    </div>

                    {measured ? (
                        <div className="text-[11px] text-gray-400">
                            {ok
                                ? `${check.node} → //${check.host}/${(check.shares || []).join(', ')} · ${check.at_text}`
                                : check.error}
                        </div>
                    ) : (
                        <div className="text-[11px] text-gray-500">
                            {say('hvTransferCheckNever', 'Not measured yet. Until it is, every VM on '
                                 + 'this host asks you to confirm that the disks might not be readable.')}
                        </div>
                    )}

                    <div className="flex items-center gap-2">
                        <select value={cluster} onChange={e => { setCluster(e.target.value); setNode(''); }}
                            className="flex-1 px-2 py-1 bg-proxmox-dark border border-proxmox-border rounded text-white text-xs">
                            <option value="">{say('hvTransferCheckCluster', 'Target cluster…')}</option>
                            {(targets || []).map(c => (
                                <option key={c.id} value={c.id}>{c.display_name || c.name}</option>
                            ))}
                        </select>
                        <select value={node} onChange={e => setNode(e.target.value)} disabled={!cluster}
                            className="flex-1 px-2 py-1 bg-proxmox-dark border border-proxmox-border rounded text-white text-xs disabled:opacity-50">
                            <option value="">{cluster && nodes.length === 0
                                ? say('hvTransferCheckLoadingNodes', 'Loading nodes…')
                                : say('hvTransferCheckNode', 'Node…')}</option>
                            {nodes.map(n => <option key={n} value={n}>{n}</option>)}
                        </select>
                        <button onClick={() => onRun(cluster, node)} disabled={!cluster || !node || busy}
                            className="px-3 py-1 rounded bg-indigo-500/10 border border-indigo-500/30 text-indigo-400 hover:text-white text-xs disabled:opacity-40">
                            {busy ? say('hvTransferChecking', 'Checking…') : say('hvTransferCheckRun', 'Check')}
                        </button>
                    </div>
                    <p className="text-[10px] text-gray-600">
                        {say('hvTransferCheckHint', 'Mounts the share read-only from that node, lists '
                             + 'it and unmounts. Nothing is written, and the disks of a particular VM '
                             + 'are still checked individually before anything is copied.')}
                    </p>
                </div>
            );
        }

        // ───────────────────────────────────────────────
        // How old what is on screen is
        // ───────────────────────────────────────────────

        /**
         * One line saying where the VM list came from and whether it is being replaced.
         *
         * PegaProx answers the Hyper-V routes from the inventory it last read, because
         * reading a full host over WinRM takes tens of seconds and no view may wait that
         * long. That makes the answer fast and sometimes old, and a table that does not
         * say which of the two it is, is worse than a slow one: somebody decides to
         * migrate a VM from rows that describe last quarter of an hour.
         *
         * Four states, and each of them is a different sentence:
         *   nothing known yet, a read running   — an empty table with a reason
         *   something known, a read running     — the age, and that it is being replaced
         *   something known, nothing running    — the age alone
         *   the last read failed                — the age, and why it has not moved
         */
        function HyperVFreshness({ state, t }) {
            if (!state) return null;
            const say = hvTranslator(t);
            const spinner = (
                <span className="w-3 h-3 rounded-full border-2 border-gray-600 border-t-transparent
                                 animate-spin shrink-0 opacity-70" />
            );

            if (state.error) {
                return (
                    <div className="flex items-start gap-2 p-3 rounded-lg border border-yellow-500/30
                                    bg-yellow-500/10 text-xs text-amber-400">
                        <div>
                            <div>{say('hvInventoryReadFailed',
                                      'The last read of this host failed. What is shown is the '
                                      + 'last state it reported:')}{' '}{state.error.message}</div>
                            {state.error.remedy && (
                                <div className="mt-0.5 text-amber-400">{state.error.remedy}</div>
                            )}
                            {state.cached && (
                                <div className="mt-0.5 text-amber-400">
                                    {hvAsOf(state, say)}
                                </div>
                            )}
                        </div>
                    </div>
                );
            }

            if (!state.cached) {
                return (
                    <div className="flex items-center gap-2 p-3 rounded-lg border border-purple-500/30
                                    bg-purple-500/10 text-xs text-indigo-400">
                        {state.refreshing && spinner}
                        {state.refreshing
                            ? say('hvInventoryReading',
                                  'Reading the host. On a host with many VMs this takes a while; '
                                  + 'the list appears here as soon as it arrives.')
                            : say('hvInventoryUnknown', 'This host has not been read yet.')}
                    </div>
                );
            }

            return (
                <div className="flex items-center gap-2 text-[11px] text-gray-500">
                    {state.refreshing && spinner}
                    <span>{hvAsOf(state, say)}</span>
                    {state.refreshing && (
                        <span>· {say('hvInventoryUpdating', 'reading the host again')}</span>
                    )}
                </div>
            );
        }

        /** "As of <clock time>", in the viewer's locale. `fetched_at` is epoch seconds. */
        function hvAsOf(state, say) {
            const at = Number(state.fetchedAt);
            if (!at || !isFinite(at)) return say('hvInventoryAsOfUnknown', 'Age unknown');
            return `${say('hvInventoryAsOf', 'As of')} ${new Date(at * 1000).toLocaleTimeString()}`;
        }

        // ───────────────────────────────────────────────
        // Registering a host
        // ───────────────────────────────────────────────

        const HYPERV_DEFAULT_CONFIG = {
            name: '', host: '', port: 5985, user: '', pass: '',
            use_ssl: false, auth: 'negotiate', encrypt_messages: true,
            ssl_verification: true, iso_library_paths: '', smb_share_map: '', smb_domain: '',
            transfer_host: '',
            max_sessions: 4,
            cluster_type: 'hyperv',
        };

        // How many calls PegaProx runs against one host at once. Mirrors the server's range
        // (hyperv_client.MIN_SESSIONS / MAX_SESSIONS), which is what actually enforces it.
        const HYPERV_SESSIONS = { min: 1, max: 8, fallback: 4 };

        // The listener Windows creates by default answers on 5985; an HTTPS listener on 5986
        // exists only where somebody set one up with a certificate. The form follows the
        // transport so an operator who changes one does not have to remember the other.
        const HYPERV_WINRM_PORTS = { http: 5985, https: 5986 };
        function hvDefaultPort(useSsl) {
            return useSsl ? HYPERV_WINRM_PORTS.https : HYPERV_WINRM_PORTS.http;
        }

        // What pypsrp accepts for a username/password login. The labels are not translated:
        // they are protocol names.
        const HYPERV_AUTH_METHODS = [
            { value: 'negotiate', label: 'Negotiate (Kerberos, then NTLM)' },
            { value: 'ntlm', label: 'NTLM' },
            { value: 'basic', label: 'Basic' },
            { value: 'kerberos', label: 'Kerberos' },
        ];

        /**
         * Turn the two free-text fields into what the API stores.
         *
         * Both are typed as text because an operator has one or two of each and a repeater
         * widget for that is more to get wrong than it saves. The parsing is here rather
         * than in the submit handler so the same rules apply on add and on reconfigure.
         */
        function hvNormaliseConfig(form) {
            const shareMap = {};
            (form.smb_share_map || '').split(/[\n,]/).forEach(entry => {
                const [drive, share] = entry.split('=').map(s => (s || '').trim());
                if (drive && share) shareMap[drive.replace(':', '').toUpperCase()] = share;
            });
            return {
                ...form,
                cluster_type: 'hyperv',
                transfer_host: (form.transfer_host || '').trim(),
                use_ssl: !!form.use_ssl,
                encrypt_messages: form.encrypt_messages !== false,
                port: parseInt(form.port, 10) || hvDefaultPort(form.use_ssl),
                max_sessions: parseInt(form.max_sessions, 10) || HYPERV_SESSIONS.fallback,
                iso_library_paths: (form.iso_library_paths || '')
                    .split('\n').map(s => s.trim()).filter(Boolean),
                smb_share_map: shareMap,
            };
        }

        function HyperVSourceForm({ config, setConfig, t }) {
            const say = hvTranslator(t);
            const field = (key, value) => setConfig({ ...config, [key]: value });
            const input = 'w-full px-4 py-2.5 bg-proxmox-dark border border-proxmox-border rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-proxmox-orange transition-colors';
            const label = 'block text-sm font-medium text-gray-300 mb-2';

            // A port the operator never touched follows the transport; one they typed stays.
            const setTransport = (useSsl) => {
                const current = parseInt(config.port, 10);
                const portFollows = !current || current === hvDefaultPort(!useSsl);
                setConfig({ ...config, use_ssl: useSsl,
                            port: portFollows ? hvDefaultPort(useSsl) : config.port });
            };
            // Basic authentication has no session key, so over HTTP there is nothing to seal
            // the payload with. The choice is shown as unavailable rather than silently ignored.
            const basicOverHttp = !config.use_ssl && config.auth === 'basic';

            return (
                <>
                    <div className="p-3 rounded-lg border border-purple-500/30 bg-purple-500/10 text-xs text-indigo-400">
                        {say('hvSourceOnly', 'A Hyper-V host is a migration source only. '
                             + 'PegaProx reads it, prepares a VM for migration, and moves that VM '
                             + 'to Proxmox. It never manages the host.')}
                    </div>

                    <div className="space-y-4">
                        <div>
                            <label className={label}>{t('clusterName') || 'Name'}</label>
                            <input type="text" required className={input} value={config.name}
                                onChange={e => field('name', e.target.value)}
                                placeholder="Hyper-V site A" />
                        </div>
                        <div>
                            <label className={label}>{t('host') || 'Host'}</label>
                            <input type="text" required className={input} value={config.host}
                                onChange={e => field('host', e.target.value)}
                                placeholder="hyperv.example.com" />
                            <p className="mt-1 text-xs text-gray-500">
                                {say('hvHostHint', 'Name or address, for WinRM. With HTTPS it has '
                                     + 'to match the certificate.')}
                            </p>
                        </div>
                        <div>
                            <label className={label}>{say('hvTransferHost', 'Address for the data transfer')}</label>
                            <input type="text" className={input} value={config.transfer_host || ''}
                                onChange={e => field('transfer_host', e.target.value)}
                                placeholder={config.host || 'same as above'} />
                            <p className="mt-1 text-xs text-gray-500">
                                {say('hvTransferHostHint', 'Where the target node mounts the disk '
                                     + 'share. Leave empty to use the address above. Management '
                                     + 'often runs over an interface that is slower than the one a '
                                     + 'transfer should take — naming the fast address here is what '
                                     + 'puts the copy on it, because the node picks the interface '
                                     + 'from its route to this address.')}
                            </p>
                        </div>
                    </div>

                    <div className="space-y-4">
                        <div>
                            <label className={label}>{say('hvTransport', 'Transport')}</label>
                            <select className={input} value={config.use_ssl ? 'https' : 'http'}
                                onChange={e => setTransport(e.target.value === 'https')}>
                                <option value="http">
                                    {say('hvTransportHttp', 'HTTP (5985)')}
                                </option>
                                <option value="https">
                                    {say('hvTransportHttps', 'HTTPS (5986)')}
                                </option>
                            </select>
                            <p className="mt-1 text-xs text-gray-500">
                                {say('hvTransportHint', 'Whatever the host is set up for: HTTP is the '
                                     + 'listener Windows creates by default, HTTPS needs a listener '
                                     + 'certificate. HTTP does not authenticate the host to PegaProx; '
                                     + 'whether that is acceptable depends on the network between the two.')}
                            </p>
                        </div>
                        <div>
                            <label className={label}>{say('hvWinrmPort', 'WinRM port')}</label>
                            <input type="number" min="1" max="65535" className={input} value={config.port}
                                onChange={e => field('port', e.target.value)}
                                placeholder={String(hvDefaultPort(config.use_ssl))} />
                            <p className="mt-1 text-xs text-gray-500">
                                {say('hvWinrmPortHint', 'Follows the transport until you set it yourself.')}
                            </p>
                        </div>
                    </div>

                    <div>
                        <label className={label}>{say('hvMaxSessions', 'Parallel sessions')}</label>
                        <input type="number" min={HYPERV_SESSIONS.min} max={HYPERV_SESSIONS.max} step="1"
                            className={input} value={config.max_sessions ?? HYPERV_SESSIONS.fallback}
                            onChange={e => field('max_sessions', e.target.value)} />
                        <p className="mt-1 text-xs text-gray-500">
                            {say('hvMaxSessionsHint', 'How many requests PegaProx sends to this host '
                                 + 'at the same time, 1 to 8. Each is its own WinRM shell on the host '
                                 + 'and counts against MaxShellsPerUser there. Requests beyond this '
                                 + 'number wait and show as waiting under Tasks.')}
                        </p>
                    </div>

                    <div className="space-y-4">
                        <div>
                            <label className={label}>{say('hvAuthMethod', 'Authentication')}</label>
                            <select className={input} value={config.auth}
                                onChange={e => field('auth', e.target.value)}>
                                {HYPERV_AUTH_METHODS.map(method => (
                                    <option key={method.value} value={method.value}>{method.label}</option>
                                ))}
                            </select>
                        </div>
                        <div>
                            <label className={label}>{t('username') || 'Username'}</label>
                            <input type="text" required className={input} value={config.user}
                                onChange={e => field('user', e.target.value)}
                                placeholder="DOMAIN\\svc-migrate" />
                            <p className="mt-1 text-xs text-gray-500">
                                {say('hvUsernameHint', 'Needs Hyper-V Administrators and Remote '
                                     + 'Management Users on the host, and read access to the share '
                                     + 'holding the disks.')}
                            </p>
                        </div>
                    </div>

                    <div>
                        <label className={label}>{t('password') || 'Password'}</label>
                        <input type="password" required={!config.editing} className={input} value={config.pass}
                            onChange={e => field('pass', e.target.value)} />
                    </div>

                    {config.use_ssl ? (
                        <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                            <input type="checkbox" checked={config.ssl_verification}
                                onChange={e => field('ssl_verification', e.target.checked)}
                                className="rounded border-gray-600" />
                            {say('hvVerifyCertificate', "Verify the host's certificate")}
                            <span className="text-xs text-gray-500">
                                {say('hvVerifyCertificateHint', '(turning this off means the connection '
                                     + 'can be read and changed in transit)')}
                            </span>
                        </label>
                    ) : (
                        <label className={'flex items-center gap-2 text-sm text-gray-300 '
                                          + (basicOverHttp ? 'opacity-60' : 'cursor-pointer')}>
                            <input type="checkbox" checked={config.encrypt_messages && !basicOverHttp}
                                disabled={basicOverHttp}
                                onChange={e => field('encrypt_messages', e.target.checked)}
                                className="rounded border-gray-600" />
                            <span className="whitespace-nowrap">{say('hvEncryptMessages', 'Encrypt the payload')}</span>
                            <span className="text-xs text-gray-500">
                                {basicOverHttp
                                    ? say('hvBasicNeedsPlain', '(Basic has no session key to encrypt '
                                          + 'with; the payload travels in clear)')
                                    : say('hvEncryptMessagesHint', '(sealed with the NTLM or Kerberos '
                                          + 'session key; turned off, the payload travels in clear)')}
                            </span>
                        </label>
                    )}

                    <div>
                        <label className={label}>{say('hvIsoLibrary', 'ISO library paths')}</label>
                        <textarea rows="2" className={input} value={config.iso_library_paths}
                            onChange={e => field('iso_library_paths', e.target.value)}
                            placeholder={'C:\\ISOs\nD:\\media\\iso'} />
                        <p className="mt-1 text-xs text-gray-500">
                            {say('hvIsoLibraryHint', 'One path per line, on the host. PegaProx '
                                 + "offers ISOs from these and does not browse the host's "
                                 + 'filesystem for others.')}
                        </p>
                    </div>

                    <div>
                        <label className={label}>{say('hvShareMap', 'Disk share mapping')}</label>
                        <textarea rows="2" className={input} value={config.smb_share_map}
                            onChange={e => field('smb_share_map', e.target.value)}
                            placeholder={'C=hyperv-disks\nD=cluster-disks'} />
                        <p className="mt-1 text-xs text-gray-500">
                            One <code>drive=share</code> per line. Without a mapping a drive is read
                            through its administrative share (C$), which needs a local administrator
                            on the host — more than reading a file should require.
                        </p>
                    </div>

                    <div>
                        <label className={label}>{say('hvShareDomain', 'Share domain')} <span className="text-gray-500">({say('optional', 'optional')})</span></label>
                        <input type="text" className={input} value={config.smb_domain}
                            onChange={e => field('smb_domain', e.target.value)} placeholder="DOMAIN" />
                        <p className="mt-1 text-xs text-gray-500">
                            {say('hvShareDomainHint',
                                 'Only needed when the username above carries no domain.')}
                        </p>
                    </div>
                </>
            );
        }

        // ───────────────────────────────────────────────
        // What a failed import left on the target
        // ───────────────────────────────────────────────

        /**
         * The durable migration record of every registered Hyper-V host, and the one
         * action that can clear it.
         *
         * This is not the migration list above it. That one lives in the server's memory
         * and is empty after a restart; this reads the database, which is exactly the
         * state somebody needs when a transfer died with the process. It shows what a
         * failed import created on the target and offers to remove it; whether to do so
         * is the operator's decision, and a new attempt is not held back either way.
         *
         * The cleanup asks twice, and the second ask names what will be deleted. It
         * touches nothing on the Hyper-V side: the source is what makes the rollback for
         * this direction "start the original again".
         */
        /**
         * What to do with a finished import, including going back.
         *
         * A migration is not over when the copy exists. Somebody has to boot it, look at
         * it, and decide. Until they do, both machines exist and only one of them may
         * run, so the panel says which is which and spells out the way back — there is no
         * button for it on purpose: the rollback is "start the original again", and the
         * only thing PegaProx would add is a chance to do it to the wrong machine.
         *
         * The part people get wrong is the third line. Everything the copy wrote since the
         * import stays on the copy. Going back means going back to the state the original
         * was in when it was read, and nothing merges the difference.
         */
        /**
         * The two steps that come after the import, in the order they have to happen.
         *
         * A guest arrives on hardware it already has drivers for — a SATA disk and an
         * emulated Intel card — because it came off Hyper-V, where nobody installed
         * anything for Proxmox's sake. That makes it bootable at once and slower than it
         * needs to be. These buttons are the rest of the way: offer the drivers, then move
         * the VM onto the faster hardware once they are in.
         *
         * The panel keeps two facts apart that look like one. "The ISO is attached" is
         * something this product can see. "The drivers are installed" is not — there is no
         * agent, no network into the guest, and nothing reads its disk — so it is recorded
         * as somebody's statement, with their name on it. Treating the first as the second
         * is how a Windows VM gets switched to VirtIO and boots to a stop code, with no way
         * back except a console and a person.
         */
