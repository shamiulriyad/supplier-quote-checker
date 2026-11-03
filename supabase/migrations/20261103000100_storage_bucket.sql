-- Private bucket for the original supplier PDFs. Run in the Supabase SQL editor, or let
-- tools/supabase_setup.py / the app create it through the Storage API.
-- public = false: objects are only reachable with the service role key (server side).
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('supplier-documents', 'supplier-documents', false, 10485760, array['application/pdf'])
on conflict (id) do update
    set public = false,
        file_size_limit = excluded.file_size_limit,
        allowed_mime_types = excluded.allowed_mime_types;
-- No storage.objects policies are created on purpose: anon and authenticated users get no access.
