        // ═══════════════════════════════════════════════
        // PegaProx — snapshot provenance (author + description)
        // CMO fork patch, issue #39
        // ═══════════════════════════════════════════════
        //
        // Every snapshot list the backend answers now carries `author` and
        // `author_origin` next to the description PVE already stored. Both may
        // be empty — PVE keeps no author, so a snapshot taken outside PegaProx
        // has none to show, and an empty field means "not known", never "you".
        //
        // These helpers are the single place those two values turn into text,
        // so a snapshot reads the same in the VM view, the cluster overview and
        // the system-wide list. Escaping is React's; the values are rendered as
        // text and never as markup.

        const SNAP_ORIGIN_AUTOMATIC = 'automatic';

        // Descriptions are free text and can be long or multi-line. They are
        // shortened for the row, never truncated away: the full text stays one
        // click behind, and in the tooltip.
        const SNAP_DESC_PREVIEW_CHARS = 120;
        const SNAP_EMPTY_MARK = '—';

        function snapshotAuthorLabel(snap, t) {
            const author = ((snap && snap.author) || '').trim();
            const origin = (snap && snap.author_origin) || '';
            const tr = (key, fallback) => (typeof t === 'function' && t(key)) || fallback;

            if (origin === SNAP_ORIGIN_AUTOMATIC) {
                const automatic = tr('snapshotAuthorAutomatic', 'Automatic');
                return author ? `${automatic} · ${author}` : automatic;
            }
            if (author) return author;
            return tr('unknown', 'Unknown');
        }

        // True when we know who made it, so the plain case can be emphasised
        // without the caller re-implementing the rule.
        function snapshotAuthorIsKnown(snap) {
            return !!(((snap && snap.author) || '').trim()) ||
                   ((snap && snap.author_origin) || '') === SNAP_ORIGIN_AUTOMATIC;
        }

        function SnapshotAuthor({ snap, t, className = '', style = {} }) {
            const known = snapshotAuthorIsKnown(snap);
            return (
                <span className={className}
                    style={{ color: known ? 'inherit' : 'var(--corp-text-muted, #728b9a)',
                             fontStyle: known ? 'normal' : 'italic',
                             overflowWrap: 'anywhere', ...style }}>
                    {snapshotAuthorLabel(snap, t)}
                </span>
            );
        }

        function SnapshotDescription({ text, t, className = '', style = {}, emptyMark = SNAP_EMPTY_MARK }) {
            const value = typeof text === 'string' ? text.trim() : '';
            const [expanded, setExpanded] = React.useState(false);
            const tr = (key, fallback) => (typeof t === 'function' && t(key)) || fallback;

            if (!value) {
                return <span className={className}
                    style={{ color: 'var(--corp-text-muted, #728b9a)', ...style }}>{emptyMark}</span>;
            }

            const needsMore = value.length > SNAP_DESC_PREVIEW_CHARS || value.indexOf('\n') !== -1;
            const shown = (!needsMore || expanded)
                ? value
                : `${value.slice(0, SNAP_DESC_PREVIEW_CHARS).trimEnd()}…`;

            return (
                <span className={className}
                    style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', ...style }}
                    title={value}>
                    {shown}
                    {needsMore && (
                        <button type="button"
                            onClick={(e) => { e.stopPropagation(); setExpanded(v => !v); }}
                            className="ml-1 underline"
                            style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer',
                                     color: 'var(--corp-accent, #49afd9)', fontSize: '11px' }}>
                            {expanded ? tr('showLess', 'less') : tr('showMore', 'more')}
                        </button>
                    )}
                </span>
            );
        }

        // Later bundle files use these directly (one shared scope); the window
        // handles are for the console and for anything loaded out of order.
        try {
            window.PegaProxSnapshotAuthor = SnapshotAuthor;
            window.PegaProxSnapshotDescription = SnapshotDescription;
            window.PegaProxSnapshotAuthorLabel = snapshotAuthorLabel;
        } catch (_) {}
