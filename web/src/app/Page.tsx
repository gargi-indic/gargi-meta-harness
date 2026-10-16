

export function Page({ title }: { title: string }) {
  return (
    <div className="container">
      <h1>{title}</h1>
      <p className="empty" style={{ marginTop: 16 }}>Not built yet.</p>
    </div>
  );
}
