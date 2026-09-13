        // ═══════════════════════════════════════════════
        // PegaProx — Hyper-V migration source (fork patch, issue #15)
        //
        // A Hyper-V host is a restricted source: PegaProx reads it, prepares a VM on it,
        // and moves that VM to Proxmox. It is never managed from here, so this file adds
        // no create, no delete and no hardware editor.
        //
        // Everything a Hyper-V source needs from the UI lives here rather than in the
        // files it appears in. Those are upstream files that every release rebuild replays
        // this patch onto, and the fewer lines of theirs this patch owns, the fewer places
        // a rebuild can go wrong. What they carry is a one-line call into this namespace.
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
        function hvMigrationSubtitle(clusters, localized) {
            const kinds = new Set((clusters || []).map(hvType));
            if (!kinds.has('hyperv')) return localized;
            const named = ['proxmox', 'xcpng', 'esxi', 'hyperv']
                .filter(kind => kinds.has(kind)).map(hvLabel);
            if (named.length < 2) return localized;
            const last = named.pop();
            return `Migrate VMs between ${named.join(', ')} and ${last}`;
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

        function hvBytesToGiB(bytes) {
            const n = Number(bytes);
            return !n || !isFinite(n) ? '0' : (n / (1024 ** 3)).toFixed(1);
        }

        // ───────────────────────────────────────────────
        // Preflight
        // ───────────────────────────────────────────────

        const HV_SEVERITY_STYLE = {
            blocking: { dot: 'bg-red-400', text: 'text-red-400', box: 'border-red-500/30 bg-red-500/5' },
            warning: { dot: 'bg-amber-400', text: 'text-amber-400', box: 'border-amber-500/30 bg-amber-500/5' },
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
        function HyperVPreflight({ report, acknowledged, onToggle }) {
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
                        <div className="text-xs font-semibold text-gray-400">Preflight</div>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                            report.blocked ? 'bg-red-500/20 text-red-400'
                                : outstanding.length ? 'bg-amber-500/20 text-amber-400'
                                    : 'bg-green-500/20 text-green-400'}`}>
                            {report.blocked ? 'Blocked'
                                : outstanding.length ? `${outstanding.length} to confirm` : 'Ready'}
                        </span>
                    </div>

                    <div className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
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
                                                    I accept this risk
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
        // Registering a host
        // ───────────────────────────────────────────────

        const HYPERV_DEFAULT_CONFIG = {
            name: '', host: '', port: 5986, user: '', pass: '',
            ssl_verification: true, iso_library_paths: '', smb_share_map: '', smb_domain: '',
            cluster_type: 'hyperv',
        };

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
                port: parseInt(form.port, 10) || 5986,
                iso_library_paths: (form.iso_library_paths || '')
                    .split('\n').map(s => s.trim()).filter(Boolean),
                smb_share_map: shareMap,
            };
        }

        function HyperVSourceForm({ config, setConfig, t }) {
            const field = (key, value) => setConfig({ ...config, [key]: value });
            const input = 'w-full px-4 py-2.5 bg-proxmox-dark border border-proxmox-border rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-proxmox-orange transition-colors';
            const label = 'block text-sm font-medium text-gray-300 mb-2';

            return (
                <>
                    <div className="p-3 rounded-lg border border-indigo-500/25 bg-indigo-500/5 text-xs text-indigo-300/90">
                        A Hyper-V host is a migration source only. PegaProx reads it, prepares a
                        VM for migration, and moves that VM to Proxmox. It never manages the host.
                    </div>

                    <div className="grid grid-cols-2 gap-4">
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
                                The name the certificate is issued for. The target node reaches the
                                disk share under this name too.
                            </p>
                        </div>
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div>
                            <label className={label}>WinRM port</label>
                            <input type="number" min="1" max="65535" className={input} value={config.port}
                                onChange={e => field('port', e.target.value)} placeholder="5986" />
                            <p className="mt-1 text-xs text-gray-500">
                                HTTPS only. The plaintext listener on 5985 is never used, because an
                                NTLM exchange and everything after it would be readable on the wire.
                            </p>
                        </div>
                        <div>
                            <label className={label}>{t('username') || 'Username'}</label>
                            <input type="text" required className={input} value={config.user}
                                onChange={e => field('user', e.target.value)}
                                placeholder="DOMAIN\\svc-migrate" />
                            <p className="mt-1 text-xs text-gray-500">
                                Needs Hyper-V Administrators and Remote Management Users on the host,
                                and read access to the share holding the disks.
                            </p>
                        </div>
                    </div>

                    <div>
                        <label className={label}>{t('password') || 'Password'}</label>
                        <input type="password" required className={input} value={config.pass}
                            onChange={e => field('pass', e.target.value)} />
                    </div>

                    <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
                        <input type="checkbox" checked={config.ssl_verification}
                            onChange={e => field('ssl_verification', e.target.checked)}
                            className="rounded border-gray-600" />
                        Verify the host's certificate
                        <span className="text-xs text-gray-500">
                            (turning this off means the connection can be read and changed in transit)
                        </span>
                    </label>

                    <div>
                        <label className={label}>ISO library paths</label>
                        <textarea rows="2" className={input} value={config.iso_library_paths}
                            onChange={e => field('iso_library_paths', e.target.value)}
                            placeholder={'C:\\ISOs\nD:\\media\\iso'} />
                        <p className="mt-1 text-xs text-gray-500">
                            One path per line, on the host. PegaProx offers ISOs from these and does
                            not browse the host's filesystem for others.
                        </p>
                    </div>

                    <div>
                        <label className={label}>Disk share mapping</label>
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
                        <label className={label}>Share domain <span className="text-gray-500">(optional)</span></label>
                        <input type="text" className={input} value={config.smb_domain}
                            onChange={e => field('smb_domain', e.target.value)} placeholder="DOMAIN" />
                        <p className="mt-1 text-xs text-gray-500">
                            Only needed when the username above carries no domain.
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
         * state somebody needs when a transfer died with the process. A failed import
         * that created a VM or volumes blocks the next attempt on that VM, because
         * starting again would copy the same disks a second time — so the panel exists to
         * make the blockage visible and to offer the only thing that resolves it.
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
        function HyperVImportResult({ row, onConsole }) {
            if (row.status !== 'completed' || !row.target_vmid) return null;
            const where = `${row.target_cluster}/${row.target_node}`;
            return (
                <div className="rounded-lg border border-proxmox-border bg-proxmox-dark/40 p-2 mt-1">
                    <div className="text-xs text-gray-300">
                        The copy is VMID {row.target_vmid} on {where}, stopped. The Hyper-V
                        original is exactly as it was found.
                    </div>
                    <button onClick={() => onConsole(row)}
                        className="mt-2 px-2 py-1 rounded bg-purple-500/20 text-purple-300 text-xs hover:bg-purple-500/30">
                        Open the copy's console for a boot test
                    </button>
                    <div className="text-[11px] text-gray-500 mt-2">
                        <div className="font-medium text-gray-400">If it does not boot:</div>
                        <ol className="list-decimal ml-4 mt-0.5 space-y-0.5">
                            <li>Stop the copy on {where}.</li>
                            <li>Start {row.source_vm_name || 'the original'} again on the Hyper-V host.</li>
                            <li>Decide later what happens to the copy — it costs storage until it is removed.</li>
                        </ol>
                        <div className="mt-1">
                            Anything the copy has written since the import stays on the copy.
                            Going back means going back to the state the original was read in,
                            and nothing merges the two. Preparation done before the migration —
                            a deleted checkpoint, a mounted ISO — is not undone by going back,
                            and a deleted checkpoint's history is gone for good.
                        </div>
                    </div>
                </div>
            );
        }

        function HyperVMigrationRecord({ clusters, apiUrl, authFetch, addToast }) {
            const hosts = (clusters || []).filter(c => hvType(c) === 'hyperv');
            const [rows, setRows] = React.useState([]);
            const [confirming, setConfirming] = React.useState(null);
            const [busy, setBusy] = React.useState(null);

            const load = React.useCallback(async () => {
                const collected = [];
                for (const host of hosts) {
                    try {
                        const resp = await authFetch(`${apiUrl}/hyperv/${host.id}/migrations`);
                        if (!resp?.ok) continue;
                        const body = await resp.json();
                        for (const row of body.migrations || []) {
                            collected.push({ ...row, cluster_id: host.id, cluster_name: host.name });
                        }
                    } catch (e) { /* a host that cannot be read simply contributes nothing */ }
                }
                collected.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
                setRows(collected);
            }, [JSON.stringify(hosts.map(h => h.id))]);

            React.useEffect(() => { load(); }, [load]);

            const cleanup = async row => {
                setBusy(row.migration_id);
                try {
                    const resp = await authFetch(
                        `${apiUrl}/hyperv/${row.cluster_id}/migrations/${row.migration_id}/cleanup`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ confirm: row.migration_id }),
                        });
                    const body = await resp.json().catch(() => ({}));
                    if (resp?.ok) {
                        addToast('Cleanup done', body.message || 'Target resources removed.', 'success');
                    } else {
                        addToast('Cleanup refused', body.error || 'The target was not changed.', 'error');
                    }
                } catch (e) {
                    addToast('Cleanup failed', e.message, 'error');
                } finally {
                    setBusy(null);
                    setConfirming(null);
                    load();
                }
            };

            // The standalone console is a URL on this same origin, so the session carries
            // over and the window survives this page being navigated away.
            const openCopyConsole = row => {
                const key = `${row.target_cluster}:qemu:${row.target_vmid}:${row.target_node}`;
                window.open(`${window.location.pathname}?console=${encodeURIComponent(key)}`,
                    '_blank', 'width=1024,height=768,menubar=no,toolbar=no');
            };

            if (!hosts.length || !rows.length) return null;

            return (
                <div className="bg-proxmox-card border border-proxmox-border rounded-xl overflow-hidden">
                    <div className="p-4 border-b border-proxmox-border flex items-center justify-between">
                        <h3 className="text-sm font-semibold text-gray-400 uppercase">
                            Hyper-V imports on record ({rows.length})
                        </h3>
                        <button onClick={load} className="text-xs text-gray-500 hover:text-white">
                            <Icons.RefreshCw className="w-3.5 h-3.5" />
                        </button>
                    </div>
                    <div className="divide-y divide-proxmox-border/50">
                        {rows.map(row => {
                            const leftovers = row.leftovers || [];
                            const isConfirming = confirming === row.migration_id;
                            return (
                                <div key={`${row.cluster_id}-${row.migration_id}`} className="p-4">
                                    <div className="flex items-center justify-between mb-1">
                                        <div className="flex items-center gap-2 min-w-0">
                                            <span className="text-sm font-medium text-white truncate">
                                                {row.source_vm_name || row.source_vmid}
                                            </span>
                                            <span className="text-[10px] text-gray-600">{row.cluster_name}</span>
                                            <span className="text-[10px] text-gray-600">{row.migration_id}</span>
                                        </div>
                                        <span className={`px-2 py-0.5 rounded text-xs font-semibold ${
                                            row.status === 'completed' ? 'bg-green-500/20 text-green-400'
                                                : row.status === 'running' ? 'bg-purple-500/20 text-purple-400'
                                                    : 'bg-red-500/20 text-red-400'}`}>
                                            {row.status}
                                        </span>
                                    </div>
                                    {row.error && (
                                        <div className="text-[11px] text-gray-500 mb-1">{row.error}</div>
                                    )}
                                    <HyperVImportResult row={row} onConsole={openCopyConsole} />
                                    {row.blocks_retry && (
                                        <div className="rounded-lg border border-amber-500/20 bg-amber-500/5 p-2 mt-1">
                                            <div className="text-xs text-amber-400">
                                                This import left {leftovers.length} resource(s) on{' '}
                                                {row.target_cluster}/{row.target_node}. Starting this VM
                                                again is blocked until they are removed or kept on purpose,
                                                so the same disks are not copied twice.
                                            </div>
                                            <div className="text-[11px] text-gray-500 mt-1">
                                                {leftovers.map(r => `${r.kind} ${r.id}`).join(', ')}
                                            </div>
                                            {isConfirming ? (
                                                <div className="flex items-center gap-2 mt-2">
                                                    <button
                                                        disabled={busy === row.migration_id}
                                                        onClick={() => cleanup(row)}
                                                        className="px-2 py-1 rounded bg-red-500 text-white text-xs font-medium disabled:opacity-50">
                                                        {busy === row.migration_id
                                                            ? 'Removing...'
                                                            : `Delete ${leftovers.map(r => r.id).join(', ')} on ${row.target_node}`}
                                                    </button>
                                                    <button onClick={() => setConfirming(null)}
                                                        className="px-2 py-1 rounded border border-proxmox-border text-gray-400 text-xs">
                                                        Keep them
                                                    </button>
                                                </div>
                                            ) : (
                                                <button onClick={() => setConfirming(row.migration_id)}
                                                    className="mt-2 px-2 py-1 rounded border border-red-500/40 text-red-400 text-xs hover:bg-red-500/10">
                                                    Remove what this import left
                                                </button>
                                            )}
                                            <div className="text-[10px] text-gray-600 mt-1">
                                                The Hyper-V source is never part of this.
                                            </div>
                                        </div>
                                    )}
                                </div>
                            );
                        })}
                    </div>
                </div>
            );
        }

        // ───────────────────────────────────────────────
        // The VM's own console
        // ───────────────────────────────────────────────

        // The Guacamole client library, loaded the first time somebody opens a console and
        // never again. It is 75 KB that only this feature needs, it is served from this
        // installation rather than from a CDN — a Proxmox management host is regularly
        // air-gapped — and a PegaProx without the console dependency never fetches it.
        const HV_CLIENT_URL = '/assets/guacamole-common.min.js';
        let hvClientPromise = null;

        function hvLoadGuacamole() {
            if (window.Guacamole) return Promise.resolve(window.Guacamole);
            if (hvClientPromise) return hvClientPromise;
            hvClientPromise = new Promise((resolve, reject) => {
                // The published build sets a global and then ends with a CommonJS export.
                // In a browser that last line throws, after everything useful has already
                // happened — a red error in the console for a library that loaded fine.
                // It gets a module object of its own for the length of the load rather
                // than a patched copy of somebody else's file.
                const hadModule = 'module' in window;
                const previous = window.module;
                window.module = { exports: {} };
                const restore = () => {
                    if (hadModule) window.module = previous; else delete window.module;
                };
                const tag = document.createElement('script');
                tag.src = HV_CLIENT_URL;
                tag.onload = () => {
                    restore();
                    window.Guacamole
                        ? resolve(window.Guacamole)
                        : reject(new Error('The console client loaded but defined nothing.'));
                };
                tag.onerror = () => {
                    restore();
                    hvClientPromise = null;
                    reject(new Error('The console client could not be loaded from this server.'));
                };
                document.head.appendChild(tag);
            });
            return hvClientPromise;
        }

        // The browser walkthrough drives this loader directly: a console is opened from a
        // VM row, and a row's console action is upstream's, so the check reaches the part
        // this patch owns without reimplementing the part it does not.
        window.hvLoadGuacamole = hvLoadGuacamole;

        function hvConsoleSocketUrl(path, token, size) {
            // The relay listens on its own port, three above the web port, so that its
            // absence costs this console and nothing else. Same host, same scheme family.
            const secure = window.location.protocol === 'https:';
            const port = Number(window.location.port || (secure ? 443 : 80)) + 3;
            const query = `token=${encodeURIComponent(token)}` +
                `&width=${size.width}&height=${size.height}&dpi=96`;
            return `${secure ? 'wss' : 'ws'}://${window.location.hostname}:${port}${path}?${query}`;
        }

        /**
         * The VMConnect console of one Hyper-V VM.
         *
         * This is the machine's console, not a remote desktop into the guest: it shows the
         * firmware, the boot loader and an operating system that has no network yet, which
         * is exactly the state somebody preparing a migration needs to see.
         *
         * The browser is given a token and an address and nothing else. The host, the
         * account and the VM's GUID stay on the server, which is also where every
         * authorization decision is made — this component cannot be talked into opening
         * somebody else's console, because it does not know how to name one.
         */
        function HyperVConsole({ clusterId, vmid, vmName, apiUrl, authFetch, onClose }) {
            const holder = React.useRef(null);
            const clientRef = React.useRef(null);
            const [state, setState] = React.useState({ phase: 'opening', error: '', detail: '' });

            React.useEffect(() => {
                let dropped = false;
                let client = null;

                (async () => {
                    let Guacamole;
                    try {
                        Guacamole = await hvLoadGuacamole();
                    } catch (e) {
                        if (!dropped) setState({ phase: 'failed', error: e.message, detail: '' });
                        return;
                    }

                    let ticket;
                    try {
                        const resp = await authFetch(
                            `${apiUrl}/hyperv/${clusterId}/vms/${vmid}/console`, { method: 'POST' });
                        const body = await resp.json().catch(() => ({}));
                        if (!resp?.ok) {
                            if (!dropped) setState({ phase: 'failed',
                                error: body.error || 'The console could not be opened.', detail: '' });
                            return;
                        }
                        ticket = body;
                    } catch (e) {
                        if (!dropped) setState({ phase: 'failed', error: e.message, detail: '' });
                        return;
                    }
                    if (dropped) return;

                    const size = {
                        width: Math.max(640, Math.round(holder.current?.clientWidth || 1024)),
                        height: Math.max(480, Math.round(holder.current?.clientHeight || 768)),
                    };
                    const tunnel = new Guacamole.WebSocketTunnel(
                        hvConsoleSocketUrl(ticket.path, ticket.token, size));
                    client = new Guacamole.Client(tunnel);
                    clientRef.current = client;

                    // A console that fails silently is worse than one that fails. Whatever
                    // the relay says — no guacd, nothing listening on the host, an account
                    // that may not use Virtual Machine Connection — is shown as it came.
                    const fail = status => {
                        if (dropped) return;
                        setState({ phase: 'failed',
                            error: status?.message || 'The console connection ended.',
                            detail: status?.code ? `status ${status.code}` : '' });
                    };
                    client.onerror = fail;
                    tunnel.onerror = fail;
                    client.onstatechange = s => {
                        // 3 is CONNECTED in this library's state enumeration.
                        if (s === 3 && !dropped) setState({ phase: 'open', error: '', detail: '' });
                    };

                    holder.current.appendChild(client.getDisplay().getElement());
                    client.connect('');

                    const keyboard = new Guacamole.Keyboard(document);
                    keyboard.onkeydown = k => client.sendKeyEvent(1, k);
                    keyboard.onkeyup = k => client.sendKeyEvent(0, k);
                    const mouse = new Guacamole.Mouse(client.getDisplay().getElement());
                    const send = mouseState => client.sendMouseState(mouseState);
                    mouse.onmousedown = mouse.onmouseup = mouse.onmousemove = send;
                    client.__pegaproxInput = { keyboard, mouse };
                })();

                return () => {
                    dropped = true;
                    const live = clientRef.current;
                    if (live) {
                        try { live.__pegaproxInput?.keyboard?.reset(); } catch (e) {}
                        try { live.disconnect(); } catch (e) {}
                    }
                };
            }, [clusterId, vmid]);

            return (
                <div className="fixed inset-0 z-50 bg-black/90 flex flex-col">
                    <div className="flex items-center justify-between px-4 py-2 bg-proxmox-card border-b border-proxmox-border">
                        <div className="flex items-center gap-2">
                            <span className="text-sm font-medium text-white">
                                {vmName || `VM ${vmid}`}
                            </span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-300">
                                Hyper-V console
                            </span>
                            <span className="text-[11px] text-gray-500">
                                the machine's own console, not a desktop session
                            </span>
                        </div>
                        <button onClick={onClose} className="text-gray-400 hover:text-white text-sm px-2">
                            Close
                        </button>
                    </div>
                    <div ref={holder} className="flex-1 overflow-auto flex items-center justify-center">
                        {state.phase === 'opening' && (
                            <div className="text-sm text-gray-400">Opening the console...</div>
                        )}
                        {state.phase === 'failed' && (
                            <div className="max-w-xl rounded-lg border border-red-500/30 bg-red-500/5 p-4">
                                <div className="text-sm text-red-400 font-medium">
                                    The console did not open
                                </div>
                                <div className="text-xs text-gray-300 mt-1">{state.error}</div>
                                {state.detail && (
                                    <div className="text-[11px] text-gray-500 mt-1">{state.detail}</div>
                                )}
                                <div className="text-[11px] text-gray-500 mt-2">
                                    Everything else in PegaProx works without the console
                                    service. See docs/hyperv-console.md for what it needs.
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            );
        }
