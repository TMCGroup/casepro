from __future__ import unicode_literals

from dash.orgs.views import OrgPermsMixin, OrgObjPermsMixin
from django.db.transaction import non_atomic_requests
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.utils.translation import ugettext_lazy as _
from django.views.generic import View
from el_pagination.paginators import LazyPaginator
from smartmin.views import SmartCRUDL, SmartTemplateView
from smartmin.views import SmartListView, SmartCreateView, SmartUpdateView, SmartDeleteView
from temba_client.utils import parse_iso8601

from casepro.cases.models import Case
from casepro.contacts.models import Contact
from casepro.rules.models import ContainsTest, GroupsTest, Quantifier
from casepro.utils import parse_csv, str_to_bool, json_encode
from casepro.utils.export import BaseDownloadView

from .forms import LabelForm
from .models import Label, Message, MessageExport, MessageFolder, Outgoing
from .tasks import message_export


class LabelFormMixin(object):
    @staticmethod
    def construct_tests(data):
        keywords = parse_csv(data['keywords'])
        groups = data['groups']
        field_test = data['field_test']

        tests = []
        if keywords:
            tests.append(ContainsTest(keywords, Quantifier.ANY))
        if groups:
            tests.append(GroupsTest(groups, Quantifier.ANY))
        if field_test:
            tests.append(field_test)

        return tests


class LabelCRUDL(SmartCRUDL):
    actions = ('create', 'update', 'delete', 'list')
    model = Label

    class Create(LabelFormMixin, OrgPermsMixin, SmartCreateView):
        form_class = LabelForm

        def get_form_kwargs(self):
            kwargs = super(LabelCRUDL.Create, self).get_form_kwargs()
            kwargs['org'] = self.request.org
            kwargs['is_create'] = True
            return kwargs

        def derive_initial(self):
            # label created manually in casepro aren't synced by default
            initial = super(LabelCRUDL.Create, self).derive_initial()
            initial['is_synced'] = False
            return initial

        def save(self, obj):
            data = self.form.cleaned_data
            org = self.request.org
            name = data['name']
            description = data['description']
            tests = self.construct_tests(data)
            is_synced = data['is_synced']

            self.object = Label.create(org, name, description, tests, is_synced)

    class Update(LabelFormMixin, OrgObjPermsMixin, SmartUpdateView):
        form_class = LabelForm

        def get_form_kwargs(self):
            kwargs = super(LabelCRUDL.Update, self).get_form_kwargs()
            kwargs['org'] = self.request.org
            kwargs['is_create'] = False
            return kwargs

        def derive_initial(self):
            initial = super(LabelCRUDL.Update, self).derive_initial()

            tests_by_type = {t.TYPE: t for t in self.object.get_tests()}
            contains_test = tests_by_type.get('contains')
            groups_test = tests_by_type.get('groups')
            field_test = tests_by_type.get('field')

            if contains_test:
                initial['keywords'] = ", ".join(contains_test.keywords)

            if groups_test:
                initial['groups'] = groups_test.groups

            if field_test:
                initial['field_test'] = field_test

            return initial

        def pre_save(self, obj):
            obj = super(LabelCRUDL.Update, self).pre_save(obj)

            tests = self.construct_tests(self.form.cleaned_data)
            obj.tests = json_encode(tests) if tests else ""

            return obj

    class Delete(OrgObjPermsMixin, SmartDeleteView):
        cancel_url = '@msgs.label_list'

        def post(self, request, *args, **kwargs):
            label = self.get_object()
            label.release()
            return HttpResponse(status=204)

    class List(OrgPermsMixin, SmartListView):
        fields = ('name', 'description', 'partners')
        default_order = ('name',)

        def derive_queryset(self, **kwargs):
            qs = super(LabelCRUDL.List, self).derive_queryset(**kwargs)
            qs = qs.filter(org=self.request.org, is_active=True)
            return qs

        def get_partners(self, obj):
            return ', '.join([p.name for p in obj.get_partners()])


class MessageSearchMixin(object):
    def derive_search(self):
        """
        Collects and prepares message search parameters into JSON serializable dict
        """
        folder = MessageFolder[self.request.GET['folder']]
        label = self.request.GET.get('label', None)
        include_archived = str_to_bool(self.request.GET.get('archived', ''))
        text = self.request.GET.get('text', None)
        contact = self.request.GET.get('contact', None)
        groups = parse_csv(self.request.GET.get('groups', ''))
        after = parse_iso8601(self.request.GET.get('after', None))
        before = parse_iso8601(self.request.GET.get('before', None))

        return {
            'folder': folder,
            'label': label,
            'include_archived': include_archived,  # only applies to flagged folder
            'text': text,
            'contact': contact,
            'groups': groups,
            'after': after,
            'before': before
        }


class MessageCRUDL(SmartCRUDL):
    actions = ('search', 'action', 'label', 'send', 'history')
    model = Message

    class Search(OrgPermsMixin, MessageSearchMixin, SmartTemplateView):
        """
        JSON endpoint for fetching messages
        """
        permission = 'orgs.org_inbox'

        def get_context_data(self, **kwargs):
            context = super(MessageCRUDL.Search, self).get_context_data(**kwargs)

            org = self.request.org
            user = self.request.user
            page = int(self.request.GET.get('page', 1))

            search = self.derive_search()
            messages = Message.search(org, user, search)
            paginator = LazyPaginator(messages, per_page=50)

            context['object_list'] = paginator.page(page)
            context['has_more'] = paginator.num_pages > page
            return context

        def render_to_response(self, context, **response_kwargs):
            return JsonResponse({
                'results': [m.as_json() for m in context['object_list']],
                'has_more': context['has_more']
            })

    class Action(OrgPermsMixin, View):
        """
        AJAX endpoint for bulk message actions. Takes a list of message ids.
        """
        permission = 'orgs.org_inbox'

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r'^message/action/(?P<action>\w+)/$'

        def post(self, request, *args, **kwargs):
            org = request.org
            user = request.user

            action = kwargs['action']

            message_ids = parse_csv(request.POST.get('messages', ''), as_ints=True)
            messages = org.incoming_messages.filter(org=org, backend_id__in=message_ids)

            label_id = int(request.POST.get('label', 0))
            label = Label.get_all(org, user).get(pk=label_id) if label_id else None

            if action == 'flag':
                Message.bulk_flag(org, user, messages)
            elif action == 'unflag':
                Message.bulk_unflag(org, user, messages)
            elif action == 'label':
                Message.bulk_label(org, user, messages, label)
            elif action == 'unlabel':
                Message.bulk_unlabel(org, user, messages, label)
            elif action == 'archive':
                Message.bulk_archive(org, user, messages)
            elif action == 'restore':
                Message.bulk_restore(org, user, messages)
            else:
                return HttpResponseBadRequest("Invalid action: %s", action)

            return HttpResponse(status=204)

    class Label(OrgPermsMixin, View):
        """
        AJAX endpoint for labelling a message.
        """
        permission = 'orgs.org_inbox'

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r'^message/label/(?P<id>\d+)/$'

        def post(self, request, *args, **kwargs):
            org = request.org
            user = request.user

            message_id = int(kwargs['id'])
            message = org.incoming_messages.filter(org=org, backend_id=message_id).first()

            label_ids = parse_csv(self.request.POST.get('labels', ''), as_ints=True)
            labels = Label.get_all(org, user).filter(pk__in=label_ids)

            message.update_labels(user, labels)

            return HttpResponse(status=204)

    class Send(OrgPermsMixin, View):
        """
        JSON endpoint for message sending. Takes a list of contact UUIDs or URNs
        """
        permission = 'orgs.org_inbox'

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r'^message/send/$'

        def post(self, request, *args, **kwargs):
            activity = request.POST['activity']
            text = request.POST['text']
            urns = parse_csv(request.POST.get('urns', ''), as_ints=False)

            contact_uuids = parse_csv(request.POST.get('contacts', ''), as_ints=False)
            contacts = Contact.objects.filter(org=request.org, uuid__in=contact_uuids)

            case_id = request.POST.get('case', None)
            case = Case.objects.get(org=request.org, pk=case_id) if case_id else None

            outgoing = Outgoing.create(request.org, request.user, activity, text, contacts, urns, case)

            return JsonResponse({'id': outgoing.pk})

    class History(OrgPermsMixin, View):
        """
        JSON endpoint for fetching message history. Takes a message backend id
        """
        permission = 'orgs.org_inbox'

        @classmethod
        def derive_url_pattern(cls, path, action):
            return r'^message/history/(?P<id>\d+)/$'

        def get(self, request, *args, **kwargs):
            message = Message.objects.get(org=request.org, backend_id=int(kwargs['id']))
            actions = [a.as_json() for a in message.get_history()]
            return JsonResponse({'actions': actions})


class MessageExportCRUDL(SmartCRUDL):
    model = MessageExport
    actions = ('create', 'read')

    class Create(OrgPermsMixin, MessageSearchMixin, SmartCreateView):
        @non_atomic_requests
        def post(self, request, *args, **kwargs):
            search = self.derive_search()
            export = MessageExport.create(self.request.org, self.request.user, search)

            message_export.delay(export.pk)

            return JsonResponse({'export_id': export.pk})

    class Read(BaseDownloadView):
        title = _("Download Messages")
        filename = 'message_export.xls'
