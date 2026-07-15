-- =====================================================================
-- CodeMorph.AI — Schema per pagamenti (pass giornaliero) e credito token
--
-- Eseguire UNA volta nell'SQL Editor di Supabase (lo script è idempotente).
-- Il backend usa la service role key e quindi bypassa la RLS; le policy in
-- fondo servono solo a permettere al frontend la LETTURA delle proprie
-- righe con il client anon/authenticated.
-- =====================================================================

-- Licenze giornaliere: tabella già usata da auth.py.
-- Creata qui SOLO se manca (per le installazioni nuove).
create table if not exists public.user_licenses (
    id          bigint generated always as identity primary key,
    user_id     uuid not null,
    expires_at  timestamptz not null,
    created_at  timestamptz not null default now()
);

create index if not exists idx_user_licenses_user_scadenza
    on public.user_licenses (user_id, expires_at desc);

-- Ordini di pagamento (PayPal Orders API v2: il bottone Google Pay del
-- JS SDK PayPal paga lo stesso ordine, cambia solo il campo `metodo`).
create table if not exists public.payment_orders (
    id            text primary key,                    -- ID ordine PayPal
    user_id       uuid not null,
    tipo          text not null check (tipo in ('pass_giornaliero', 'ricarica_token')),
    metodo        text not null check (metodo in ('paypal', 'googlepay')),
    importo_eur   numeric(10, 2) not null check (importo_eur > 0),
    valuta        text not null default 'EUR',
    stato         text not null default 'creato' check (stato in ('creato', 'completato', 'anomalo')),
    created_at    timestamptz not null default now(),
    completed_at  timestamptz
);

create index if not exists idx_payment_orders_user
    on public.payment_orders (user_id, created_at desc);

-- Portafoglio del credito token (in EUR). Il pass giornaliero accredita
-- la quota inclusa (20 €); ogni fase addebita il consumo reale.
create table if not exists public.token_wallets (
    user_id     uuid primary key,
    saldo_eur   numeric(12, 4) not null default 0,
    updated_at  timestamptz not null default now()
);

-- Movimenti del credito token (audit trail di accrediti e consumi).
create table if not exists public.token_transactions (
    id                 bigint generated always as identity primary key,
    user_id            uuid not null,
    session_id         text,
    tipo               text not null check (tipo in ('accredito_pass', 'ricarica', 'consumo')),
    importo_eur        numeric(12, 4) not null,   -- positivo = accredito, negativo = consumo
    tokens_prompt      bigint,
    tokens_completion  bigint,
    tokens_totali      bigint,
    modello            text,
    descrizione        text,
    created_at         timestamptz not null default now()
);

create index if not exists idx_token_transactions_user
    on public.token_transactions (user_id, created_at desc);

-- Aggiornamento ATOMICO del saldo: evita le race condition dei
-- read-modify-write concorrenti. Il backend la invoca via supabase.rpc().
create or replace function public.modifica_saldo_token(p_user_id uuid, p_delta numeric)
returns numeric
language plpgsql
security definer
set search_path = public
as $$
declare
    nuovo_saldo numeric;
begin
    insert into public.token_wallets (user_id, saldo_eur)
    values (p_user_id, 0)
    on conflict (user_id) do nothing;

    update public.token_wallets
       set saldo_eur = saldo_eur + p_delta,
           updated_at = now()
     where user_id = p_user_id
 returning saldo_eur into nuovo_saldo;

    return nuovo_saldo;
end;
$$;

-- CRITICO: PostgREST espone le funzioni di `public` anche ai ruoli anon e
-- authenticated. Senza questa revoca chiunque potrebbe accreditarsi token
-- da solo chiamando la RPC con il proprio JWT. Deve restare invocabile
-- SOLO dal backend (service role).
revoke all on function public.modifica_saldo_token(uuid, numeric) from public;
revoke all on function public.modifica_saldo_token(uuid, numeric) from anon;
revoke all on function public.modifica_saldo_token(uuid, numeric) from authenticated;

-- RLS: scritture solo dal backend (service role, che bypassa la RLS);
-- il frontend può solo LEGGERE le proprie righe.
alter table public.payment_orders     enable row level security;
alter table public.token_wallets      enable row level security;
alter table public.token_transactions enable row level security;

drop policy if exists "lettura propri ordini" on public.payment_orders;
create policy "lettura propri ordini"
    on public.payment_orders for select
    using (auth.uid() = user_id);

drop policy if exists "lettura proprio saldo" on public.token_wallets;
create policy "lettura proprio saldo"
    on public.token_wallets for select
    using (auth.uid() = user_id);

drop policy if exists "lettura propri movimenti" on public.token_transactions;
create policy "lettura propri movimenti"
    on public.token_transactions for select
    using (auth.uid() = user_id);
