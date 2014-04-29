from __future__ import unicode_literals
import re
from django.conf import settings
import datetime
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.core.exceptions import ValidationError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.views.generic import ListView
from . import models
import json
import jwt
import hashlib
import logging
from django.core.urlresolvers import reverse
import base64


# From Django 1.6 - not present in 1.4.  django.utils.http.urlsafe_...
def urlsafe_base64_encode(s):
    """
    Encodes a bytestring in base64 for use in URLs, stripping any trailing
    equal signs.
    """
    return base64.urlsafe_b64encode(s).rstrip(b'\n=')

def urlsafe_base64_decode(s):
    """
    Decodes a base64 encoded string, adding back any trailing equal signs that
    might have been stripped.
    """
    s = s.encode('utf-8') # base64encode should only return ASCII.
    try:
        return base64.urlsafe_b64decode(s.ljust(len(s) + len(s) % 4, b'='))
    except (LookupError, BinasciiError) as e:
        raise ValueError(e)


decode_param = urlsafe_base64_decode
encode_param = urlsafe_base64_encode


logger = logging.getLogger(__name__)


def hello(request):
    return HttpResponse("Hello, world.  Badges!!!")


def should_skip_jwt_auth():
    return getattr(settings, 'BADGEKIT_SKIP_JWT_AUTH', False)


def get_jwt_key():
    key = getattr(settings, 'BADGEKIT_JWT_KEY', None)
    if not key:
        logger.error('Got a webhook request, but no BADGEKIT_JWT_KEY configured! Rejecting.')
        raise jwt.DecodeError('No JWT Key')
    return key


auth_header_re = re.compile(r'JWT token="([0-9A-Za-z-_.]+)"')


@require_POST
@csrf_exempt
def badge_issued_hook(request):
    if not should_skip_jwt_auth():
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header:
            return HttpResponse('JWT auth required', status=401)
        match = auth_header_re.match(auth_header)
        if not match:
            logging.info("Bad auth header: <<%s>>" % repr(auth_header))
            return HttpResponse('Malformed Authorization header', status=403)

        auth_token = match.group(1)

        try:
            payload = jwt.decode(auth_token, key=get_jwt_key())
            body_sig = payload['body']['hash']
            # Assuming sha256 for now.
            if body_sig != hashlib.sha256(request.body).hexdigest():
                # Timing attack shouldn't matter: attacker can see the sig anyway.
                return HttpResponse('Bad body signature', status=403)
            # TODO: test method, etc.

        except (jwt.DecodeError, KeyError):
            #logging.exception('Bad JWT auth')
            return HttpResponse('Bad JWT auth', status=403)

    try:
        data = json.loads(request.body.decode(request.encoding or 'utf-8'))
        expected_keys = set(['action', 'uid', 'email', 'assertionUrl', 'issuedOn'])
        if type(data) != dict or set(data.keys()) != expected_keys:
            return HttpResponseBadRequest("Unexpected or Missing Fields")

        data['issuedOn'] = datetime.datetime.fromtimestamp(data['issuedOn'])
        del data['action']

        obj = models.BadgeInstanceNotification.objects.create(**data)
        obj.full_clean() # throws ValidationError if fields are bad.
        obj.save()

        models.badge_instance_issued.send_robust(obj, **data)
    except (ValueError, TypeError, ValidationError) as e:
        return HttpResponseBadRequest("Bad JSON request: %s" % str(e))

    return HttpResponse(json.dumps({"status": "ok"}), content_type="application/json")


class InstanceListView(ListView):
    model = models.BadgeInstanceNotification


def create_claim_url(assertionUrl):
    return reverse('badgekit_webhooks.views.claim_page',
            args=[encode_param(assertionUrl)])


def claim_page(request, b64_assertion_url):
    assertionUrl = decode_param(b64_assertion_url)
    # TODO might want to validate the URL against a whitelist - there might be
    # no security issue, but it makes me uneasy not to...

    return render(request, 'badgekit_webhooks/claim_page.html', {
        'assertionUrl': assertionUrl,
        })
