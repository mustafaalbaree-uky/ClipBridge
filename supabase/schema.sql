-- ClipBridge database schema.
-- Run this in the Supabase SQL editor of a fresh project.

create table if not exists clips (
    id         bigint generated always as identity primary key,
    content    text not null,
    source     text,
    created_at timestamptz not null default now(),
    expires_at timestamptz
);

-- The anon key is the only credential the clients hold, so the anon role
-- needs read and insert access. Keep the anon key private: anyone who has
-- it can read the clips you share while they are still live.
alter table clips enable row level security;

-- Policies decide which rows a caller may touch. They do not decide which
-- statements a caller may run, and the two are separate: a grant of TRUNCATE
-- is not filtered by any policy, so a single statement would empty the table
-- no matter what the policies say. Supabase hands new tables a broad default
-- grant, so name the three statements the policies below actually govern and
-- take back everything else.
revoke all on clips from anon;
grant select, insert, delete on clips to anon;

-- Reads are limited to clips that have not expired yet. A clip that ages
-- out is unreadable even though the row is still there, which is what keeps
-- the exposure window short.
create policy "anon can read live clips"
    on clips for select to anon using (expires_at > now());

create policy "anon can insert clips"
    on clips for insert to anon with check (true);

-- Anyone may clear out what has already expired, so the table drains even
-- when no new clip arrives to run the rolling buffer below.
create policy "anon can delete expired clips"
    on clips for delete to anon using (expires_at < now());

-- Hold clips for fifteen minutes, whatever the client asked for. The clients
-- send a 24 hour expiry and this overrules them, so the cap is one number in
-- one place rather than a promise five clients have to keep.
--
-- Fifteen minutes means the table is empty most of the time. That is the
-- intended state, and both desktop clients now seed their poll cursor from an
-- empty table without swallowing the next clip to arrive. If you shorten this
-- further, keep it comfortably longer than poll_seconds.
create or replace function clamp_clip_expiry()
returns trigger
language plpgsql
as $$
declare
    cap constant interval := interval '15 minutes';
begin
    if new.expires_at is null or new.expires_at > now() + cap then
        new.expires_at := now() + cap;
    end if;
    return new;
end;
$$;

drop trigger if exists clips_clamp_expiry on clips;
create trigger clips_clamp_expiry
    before insert on clips
    for each row execute function clamp_clip_expiry();

-- Rolling buffer, and the drain. Every insert first drops whatever has
-- expired, so the table empties itself without a scheduler to run and watch,
-- then caps what is left at the 50 most recent.
-- Optional iPhone push: if you use the Bark app, uncomment the block below
-- and paste in your own Bark device key. Requires the pg_net extension
-- (Database -> Extensions -> pg_net).
create or replace function on_clip_insert()
returns trigger
language plpgsql
security definer
as $$
begin
    -- security definer above is load bearing, and it went missing from the
    -- live database once. Without it this runs as the caller, which is anon,
    -- and both statements below come back under row level security: the
    -- subquery can only see clips that are still live, and the delete can
    -- only touch ones that have expired. The buffer then stops capping
    -- anything and the table grows without limit. It reached 145 rows that
    -- way. Owned by postgres and running as postgres, it sees the whole table.
    delete from clips where expires_at < now();

    delete from clips
    where id not in (
        select id from clips order by created_at desc limit 50
    );

    -- if new.source = 'pc' then
    --     perform net.http_post(
    --         url     := 'https://api.day.app/push',
    --         headers := '{"Content-Type": "application/json"}'::jsonb,
    --         body    := jsonb_build_object(
    --             'device_key', 'YOUR-BARK-DEVICE-KEY',
    --             'title', 'Clip from PC',
    --             'body', left(new.content, 100),
    --             'copy', new.content,
    --             'url', 'shortcuts://run-shortcut?name=PC%20Transcribe'
    --         )
    --     );
    -- end if;

    return new;
end;
$$;

drop trigger if exists clips_after_insert on clips;
create trigger clips_after_insert
    after insert on clips
    for each row execute function on_clip_insert();
