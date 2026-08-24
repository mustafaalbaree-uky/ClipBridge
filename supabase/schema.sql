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
-- it can read your clipboard history.
alter table clips enable row level security;

create policy "anon can read clips"
    on clips for select to anon using (true);

create policy "anon can insert clips"
    on clips for insert to anon with check (true);

-- Rolling buffer: keep only the 50 most recent clips.
-- Optional iPhone push: if you use the Bark app, uncomment the block below
-- and paste in your own Bark device key. Requires the pg_net extension
-- (Database -> Extensions -> pg_net).
create or replace function on_clip_insert()
returns trigger
language plpgsql
security definer
as $$
begin
    delete from clips
    where id not in (
        select id from clips order by created_at desc limit 50
    );

    -- if new.source = 'pc' then
    --     perform net.http_post(
    --         url  := 'https://api.day.app/push',
    --         body := jsonb_build_object(
    --             'device_key', 'YOUR-BARK-DEVICE-KEY',
    --             'title', 'Clip from PC',
    --             'body', left(new.content, 100),
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
