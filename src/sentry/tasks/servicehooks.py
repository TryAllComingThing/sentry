from __future__ import absolute_import, print_function

import six

from time import time

from sentry.api.serializers import serialize, app_platform_event
from sentry.http import safe_urlopen
from sentry.models import Group, SentryAppInstallation, ServiceHook
from sentry.tasks.base import instrumented_task, retry
from sentry.utils import json
from sentry.utils.http import absolute_uri

# This is an extra, explicit, measure to ensure we only send events for
# resource changes we deem necessary.
ALLOWED_ACTIONS = (
    'issue.created',
)

# We call some models by a different name, publically, than their class name.
# For example the model Group is called "Issue" in the UI. We want the Service
# Hook events to match what we externally call these primitives.
RESOURCE_RENAMES = {
    'Group': 'issue',
}


@instrumented_task(
    'sentry.tasks.process_resource_change',
    default_retry_delay=60 * 5,
    max_retries=5,
)
@retry()
def process_resource_change(sender, instance_id, created):
    model = sender.__name__
    model = RESOURCE_RENAMES.get(model, model.lower())

    instance = sender.objects.get(id=instance_id)

    event = 'created' if created else 'updated'
    action = u'{}.{}'.format(model, event)

    if action not in ALLOWED_ACTIONS:
        return

    project = None

    if isinstance(instance, Group):
        project = instance.project

    if not project:
        return

    servicehooks = ServiceHook.objects.filter(
        project_id=project.id,
    )

    for servicehook in filter(lambda s: action in s.events, servicehooks):
        # For now, these ``post_save`` callbacks are only valid for service
        # hooks created by a Sentry App.
        if not servicehook.created_by_sentry_app:
            continue

        payload = app_platform_event(
            action,
            SentryAppInstallation.objects.get(id=servicehook.actor_id),
            serialize(instance),
        )

        send_request(servicehook, payload)


def send_request(servicehook, payload):
    from sentry import tsdb
    tsdb.incr(tsdb.models.servicehook_fired, servicehook.id)

    headers = {
        'Content-Type': 'application/json',
        'X-ServiceHook-Timestamp': six.text_type(int(time())),
        'X-ServiceHook-GUID': servicehook.guid,
        'X-ServiceHook-Signature': servicehook.build_signature(json.dumps(payload)),
    }

    safe_urlopen(
        url=servicehook.url,
        data=json.dumps(payload),
        headers=headers,
        timeout=5,
        verify_ssl=True,
    )


def get_payload_v0(event):
    from sentry.api.serializers import serialize

    group = event.group
    project = group.project

    project_url_base = absolute_uri(u'/{}/{}'.format(
        project.organization.slug,
        project.slug,
    ))

    group_context = serialize(group)
    group_context['url'] = u'{}/issues/{}/'.format(
        project_url_base,
        group.id,
    )

    event_context = serialize(event)
    event_context['url'] = u'{}/issues/{}/events/{}/'.format(
        project_url_base,
        group.id,
        event.id,
    )
    data = {
        'project': {
            'slug': project.slug,
            'name': project.name,
        },
        'group': group_context,
        'event': event_context,
    }
    return data


@instrumented_task(
    name='sentry.tasks.process_service_hook', default_retry_delay=60 * 5, max_retries=5
)
def process_service_hook(servicehook_id, event, **kwargs):
    try:
        servicehook = ServiceHook.objects.get(id=servicehook_id)
    except ServiceHook.DoesNotExist:
        return

    if servicehook.version == 0:
        payload = get_payload_v0(event)
    else:
        raise NotImplementedError

    send_request(servicehook, payload)
