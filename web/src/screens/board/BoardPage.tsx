import { useSearchParams, Link } from 'react-router-dom';
import { useBoard, useCampaigns } from '../../api/queries';
import { StatusTag } from '../../ui/StatusTag';
import { Card as CardType } from '../../api/types';
import './board.css';

function BoardCard({ card, hideRepo }: { card: CardType; hideRepo?: boolean }) {
  return (
    <article className="board-card">
      <div className="mono board-card-id">
        {!hideRepo ? `${card.repo}#${card.issue}` : `#${card.issue}`}
      </div>
      <div className="board-card-title">{card.title}</div>
      <div className="board-card-meta mt-8">
        <StatusTag tone="muted">{card.agent}</StatusTag>
        <span className="mono board-card-playbook">{card.playbook}</span>
      </div>
      <div className="board-card-links mt-8">
        {card.pr && (
          <a href={`https://github.com/${card.repo}/pull/${card.pr}`} target="_blank" rel="noreferrer">
            PR {card.pr}
          </a>
        )}
        {card.pr && card.run_id && " · "}
        {card.run_id && (
          <Link to={`/runs/${encodeURIComponent(card.run_id)}`}>Run</Link>
        )}
      </div>
      {card.blocked_by.length > 0 && (
        <div className="mono board-card-blocked mt-8">
          blocked by {card.blocked_by.map(b => `#${b}`).join(', ')}
        </div>
      )}
    </article>
  );
}

export function BoardPage() {
  const [searchParams] = useSearchParams();
  const campaign = searchParams.get('campaign') || undefined;

  const campaignsQuery = useCampaigns();
  const boardQuery = useBoard(campaign);

  const columns = [
    { key: "ready", label: "READY" },
    { key: "running", label: "RUNNING" },
    { key: "needs_you", label: "NEEDS YOU" },
    { key: "review", label: "REVIEW" },
    { key: "blocked", label: "BLOCKED" },
    { key: "done", label: "DONE" },
    { key: "failed", label: "FAILED" },
  ] as const;

  if (boardQuery.isLoading || campaignsQuery.isLoading) return null;
  const data = boardQuery.data;

  return (
    <div className="board-page">
      <div className="board-selector">
        <ul className="campaign-list">
          <li>
            <Link to="/board" className={!campaign ? "active" : ""}>All</Link>
          </li>
          {campaignsQuery.data?.map(c => (
            <li key={c.id}>
              <Link
                to={`/board?campaign=${encodeURIComponent(c.repo)}`}
                className={c.repo === campaign ? "active" : ""}
              >
                {c.repo}
              </Link>
            </li>
          ))}
        </ul>
      </div>

      <div className="board-container">
        <div className="board-grid">
          {columns.map(col => {
            const cards = data?.columns[col.key] || [];
            return (
              <div key={col.key} className="board-column">
                <div className="mono board-column-header">
                  {col.label} {cards.length}
                </div>
                <div className="board-column-cards">
                  {cards.length === 0 ? (
                    <div className="mono board-column-empty">—</div>
                  ) : (
                    cards.map(card => (
                      <BoardCard
                        key={`${card.repo}#${card.issue}`}
                        card={card}
                        hideRepo={!!campaign}
                      />
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
