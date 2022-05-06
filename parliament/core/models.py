# coding: utf-8

import datetime
import re

from django.conf import settings
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core import urlresolvers
from django.db import models
from django.template.defaultfilters import slugify
from django.utils.safestring import mark_safe

import lxml.etree
import lxml.html
from markdown import markdown
import requests

from parliament.core import parsetools
from parliament.core import thumbnail # importing so it'll register a tag
from parliament.core.utils import memoize_property, ActiveManager, language_property

import logging
logger = logging.getLogger(__name__)

POL_AFFIL_ID_LOOKUP_URL = 'https://www.ourcommons.ca/Parliamentarians/en/members/profileredirect?affiliationId=%s'
POL_PERSON_ID_LOOKUP_URL = 'https://www.ourcommons.ca/Members/en/openparliamentdotca-lookup(%s)'

class InternalXref(models.Model):
    """A general-purpose table for quickly storing internal links."""
    text_value = models.CharField(max_length=250, blank=True, db_index=True)
    int_value = models.IntegerField(blank=True, null=True, db_index=True)
    target_id = models.IntegerField(db_index=True)
    
    # CURRENT SCHEMAS
    # party_names
    # bill_callbackid
    # session_legisin -- LEGISinfo ID for a session
    # edid_postcode -- the EDID -- which points to a riding, but is NOT the primary  key -- for a postcode
    schema = models.CharField(max_length=15, db_index=True)
    
    def __unicode__(self):
        return u"%s: %s %s for %s" % (self.schema, self.text_value, self.int_value, self.target_id)

class PartyManager(models.Manager):
    
    def get_by_name(self, name):
        x = InternalXref.objects.filter(schema='party_names', text_value=name.strip().lower())
        if len(x) == 0:
            raise Party.DoesNotExist()
        elif len(x) > 1:
            raise Exception("More than one party matched %s" % name.strip().lower())
        else:
            return self.get_queryset().get(pk=x[0].target_id)
            
class Party(models.Model):
    """A federal political party."""
    name_en = models.CharField(max_length=100)
    name_fr = models.CharField(max_length=100, blank=True)
    
    short_name_en = models.CharField(max_length=100, blank=True)
    short_name_fr = models.CharField(max_length=100, blank=True)

    slug = models.CharField(max_length=10, blank=True)
    
    name = language_property('name')
    short_name = language_property('short_name')

    objects = PartyManager()
    
    class Meta:
        verbose_name_plural = 'Parties'

    def __init__(self, *args, **kwargs):
        # If we're creating a new object, set a flag to save the name to the alternate-names table.
        super(Party, self).__init__(*args, **kwargs)
        self._saveAlternate = True

    def save(self):
        if not self.name_fr:
            self.name_fr = self.name_en
        if not self.short_name_en:
            self.short_name_en = self.name_en
        if not self.short_name_fr:
            self.short_name_fr = self.name_fr
        super(Party, self).save()
        if getattr(self, '_saveAlternate', False):
            self.add_alternate_name(self.name_en)
            self.add_alternate_name(self.name_fr)

    def delete(self):
        InternalXref.objects.filter(schema='party_names', target_id=self.id).delete()
        super(Party, self).delete()

    def add_alternate_name(self, name):
        name = name.strip().lower()
        # check if exists
        x = InternalXref.objects.filter(schema='party_names', text_value=name)
        if len(x) == 0:
            InternalXref(schema='party_names', target_id=self.id, text_value=name).save()
        else:
            if x[0].target_id != self.id:
                raise Exception("Name %s already points to a different party" % name.strip().lower())
                
    def __unicode__(self):
        return self.name

class Person(models.Model):
    """Abstract base class for models representing a person."""
    
    name = models.CharField(max_length=100)
    name_given = models.CharField("Given name", max_length=50, blank=True)
    name_family = models.CharField("Family name", max_length=50, blank=True)

    def __unicode__(self):
        return self.name
    
    class Meta:
        abstract = True
        ordering = ('name',)

class PoliticianManager(models.Manager):
    
    def elected(self):
        """Returns a QuerySet of all politicians that were once elected to office."""
        return self.get_queryset().annotate(
            electedcount=models.Count('electedmember')).filter(electedcount__gte=1)
            
    def never_elected(self):
        """Returns a QuerySet of all politicians that were never elected as MPs.
        
        (at least during the time period covered by our database)"""
        return self.get_queryset().filter(electedmember__isnull=True)
        
    def current(self):
        """Returns a QuerySet of all current MPs."""
        return self.get_queryset().filter(electedmember__end_date__isnull=True,
            electedmember__start_date__isnull=False).distinct()
        
    def elected_but_not_current(self):
        """Returns a QuerySet of former MPs."""
        return self.get_queryset().exclude(electedmember__end_date__isnull=True)
    
    def filter_by_name(self, name):
        """Returns a list of politicians matching a given name."""
        return [i.politician for i in 
            PoliticianInfo.sr_objects.filter(schema='alternate_name', value=parsetools.normalizeName(name))]
    
    def get_by_name(self, name, session=None, riding=None, election=None, party=None, saveAlternate=True, strictMatch=False):
        """ Return a Politician by name. Uses a bunch of methods to try and deal with variations in names.
        If given any of a session, riding, election, or party, returns only politicians who match.
        If given session and optinally riding, tries to match the name more laxly.
        
        saveAlternate: If we have Thomas Mulcair and we match, via session/riding, to Tom Mulcair, save Tom
            in the alternate names table
        strictMatch: Even if given a session, don't try last-name-only matching.
        
        """
        
        # Alternate names for a pol are in the InternalXref table. Assemble a list of possibilities
        poss = PoliticianInfo.sr_objects.filter(schema='alternate_name', value=parsetools.normalizeName(name))
        if len(poss) >= 1:
            # We have one or more results
            if session or riding or party:
                # We've been given extra criteria -- see if they match
                result = None
                for p in poss:
                    # For each possibility, assemble a list of matching Members
                    members = ElectedMember.objects.filter(politician=p.politician)
                    if riding: members = members.filter(riding=riding)
                    if session: members = members.filter(sessions=session)
                    if party: members = members.filter(party=party)
                    if len(members) >= 1:
                        if result: # we found another match on a previous journey through the loop
                            # can't disambiguate, raise exception
                            raise Politician.MultipleObjectsReturned(name)
                        # We match! Save the result.
                        result = members[0].politician
                if result:
                    return result
            elif election:
                raise Exception("Election not implemented yet in Politician get_by_name")
            else:
                # No extra criteria -- return what we got (or die if we can't disambiguate)
                if len(poss) > 1:
                    raise Politician.MultipleObjectsReturned(name)
                else:
                    return poss[0].politician
        if session and not strictMatch:
            # We couldn't find the pol, but we have the session and riding, so let's give this one more shot
            # We'll try matching only on last name
            match = re.search(r'\s([A-Z][\w-]+)$', name.strip()) # very naive lastname matching
            if match:
                lastname = match.group(1)
                pols = self.get_queryset().filter(name_family=lastname, electedmember__sessions=session).distinct()
                if riding:
                    pols = pols.filter(electedmember__riding=riding)
                if len(pols) > 1:
                    if riding:
                        raise Exception("DATA ERROR: There appear to be two politicians with the same last name elected to the same riding from the same session... %s %s %s" % (lastname, session, riding))
                elif len(pols) == 1:
                    # yes!
                    pol = pols[0]
                    if saveAlternate:
                        pol.add_alternate_name(name) # save the name we were given as an alternate
                    return pol
        raise Politician.DoesNotExist("Could not find politician named %s" % name)

    def get_by_slug_or_id(self, slug_or_id):
        if slug_or_id.isdigit():
            return self.get(id=slug_or_id)
        return self.get(slug=slug_or_id)

    def get_by_parl_mp_id(self, parlid, session=None, riding_name=None):
        """
        Find a Politician object, based on the ourcommons.ca person ID.
        """
        try:
            info = PoliticianInfo.sr_objects.get(schema='parl_mp_id', value=unicode(parlid))
            return info.politician
        except PoliticianInfo.DoesNotExist:
            pol, x_mp_id = self._get_pol_from_ourcommons_url(POL_PERSON_ID_LOOKUP_URL % parlid,
                session, riding_name)
            if int(parlid) != x_mp_id:
                raise Exception("get_by_parl_mp_id: Get for ID %s found ID %s (%s)" %
                    (parlid, x_mp_id, pol))
            pol.set_info('parl_mp_id', parlid, overwrite=False)
            return self.get_queryset().get(id=pol.id)
            
    def get_by_parl_affil_id(self, parlid, session=None, riding_name=None):
        """
        Find a Politician object, based on one of Parliament's affiliation IDs.
        These are internal person-in-role IDs that are not, as far as I know,
        very well exposed. Notably these are the IDs that we get in Hansard XML.
        """
        try:
            info = PoliticianInfo.sr_objects.get(
                schema='parl_affil_id', value=unicode(parlid))
            return info.politician
        except PoliticianInfo.DoesNotExist:
            pol, parl_mp_id = self._get_pol_from_ourcommons_url(POL_AFFIL_ID_LOOKUP_URL % parlid,
                                                             session, riding_name)
            try:
                mpid_info = PoliticianInfo.objects.get(schema='parl_mp_id', value=parl_mp_id)
                if mpid_info.politician_id != pol.id:
                    raise Exception("get_by_parl_affil_id: for ID %s found %s, but mp_id %s already used for %s"
                        % (parlid, pol, parl_mp_id, mpid_info.politician))
            except PoliticianInfo.DoesNotExist:
                pol.set_info('parl_mp_id', parl_mp_id, overwrite=False)
            
            pol.set_info_multivalued('parl_affil_id', parlid)
            return self.get_queryset().get(id=pol.id)

    def _get_pol_from_ourcommons_url(self, url, session=None, riding_name=None):
        try:
            initial_resp = requests.get(url)
            initial_resp.raise_for_status()
        except requests.HTTPError:
            raise Politician.DoesNotExist("Couldn't open " + url)

        xml_url = initial_resp.url
        url_match = re.search(r'\((\d+)\)$', xml_url)
        if not url_match:
            if xml_url.endswith('Members/en'):
                raise Politician.DoesNotExist("ourcommons redirect doesn't recognize that ID")
            raise Exception("Apparent change in ourcommons URL scheme? %s" % xml_url)
        parl_mp_id = int(url_match.group(1))
        xml_url += '/xml'
        xml_resp = requests.get(xml_url)
        xml_resp.raise_for_status()
        xml_doc = lxml.etree.fromstring(xml_resp.content)

        polname = xml_doc.findtext('MemberOfParliamentRole/PersonOfficialFirstName'
            ) + ' ' + xml_doc.findtext('MemberOfParliamentRole/PersonOfficialLastName')
        polriding = xml_doc.findtext('MemberOfParliamentRole/ConstituencyName')
                    
        try:
            riding = Riding.objects.get_by_name(polriding)
        except Riding.DoesNotExist:
            raise Politician.DoesNotExist("Couldn't find riding %s" % polriding)
        if riding_name and riding != Riding.objects.get_by_name(riding_name):
            raise Exception("Pol get_by_id sanity check failed: XML riding %s doesn't match provided name %s"
                % (polriding, riding_name))
        if session:
            pol = self.get_by_name(name=polname, session=session, riding=riding)
        else:
            pol = self.get_by_name(name=polname, riding=riding)
        return (pol, parl_mp_id)

class Politician(Person):
    """Someone who has run for federal office."""
    GENDER_CHOICES = (
        ('M', 'Male'),
        ('F', 'Female'),
    )

    WORDCLOUD_PATH = 'autoimg/wordcloud-pol'

    dob = models.DateField(blank=True, null=True)
    gender = models.CharField(max_length=1, blank=True, choices=GENDER_CHOICES)
    headshot = models.ImageField(upload_to='polpics', blank=True, null=True)
    slug = models.CharField(max_length=30, blank=True, db_index=True)
    
    objects = PoliticianManager()

    def to_api_dict(self, representation):
        d = dict(
            name=self.name
        )
        if representation == 'detail':
            info = self.info_multivalued()
            members = list(self.electedmember_set.all().select_related('party', 'riding').order_by('-end_date'))
            d.update(
                given_name=self.name_given,
                family_name=self.name_family,
                gender=self.get_gender_display().lower(),
                image=self.headshot.url if self.headshot else None,
                other_info=info,
                links=[]
            )
            if 'email' in info:
                d['email'] = info.pop('email')[0]
            if self.parlpage:
                d['links'].append({
                    'url': self.parlpage,
                    'note': 'Page on parl.gc.ca'
                })
            if 'web_site' in info:
                d['links'].append({
                    'url': info.pop('web_site')[0],
                    'note': 'Official site'
                })
            if 'phone' in info:
                d['voice'] = info.pop('phone')[0]
            if 'fax' in info:
                d['fax'] = info.pop('fax')[0]
            d['memberships'] = [
                member.to_api_dict('detail', include_politician=False)
                for member in members
            ]
        return d

    def add_alternate_name(self, name):
        normname = parsetools.normalizeName(name)
        if normname not in self.alternate_names():
            self.set_info_multivalued('alternate_name', normname)

    def alternate_names(self):
        """Returns a list of ways of writing this politician's name."""
        return self.politicianinfo_set.filter(schema='alternate_name').values_list('value', flat=True)
        
    def add_slug(self):
        """Assigns a slug to this politician, unless there's a conflict."""
        if self.slug:
            return True
        slug = slugify(self.name)
        if Politician.objects.filter(slug=slug).exists():
            logger.warning("Slug %s already taken" % slug)
            return False
        self.slug = slug
        self.save()
        
    @property
    @memoize_property
    def current_member(self):
        """If this politician is a current MP, returns the corresponding ElectedMember object.
        Returns False if the politician is not a current MP."""
        try:
            return ElectedMember.objects.get(politician=self, end_date__isnull=True)
        except ElectedMember.DoesNotExist:
            return False

    @property
    @memoize_property        
    def latest_member(self):
        """If this politician has been an MP, returns the most recent ElectedMember object.
        Returns None if the politician has never been elected."""
        try:
            return ElectedMember.objects.filter(politician=self).order_by('-start_date').select_related('party', 'riding')[0]
        except IndexError:
            return None

    @property
    @memoize_property
    def latest_candidate(self):
        """Returns the most recent Candidacy object for this politician.
        Returns None if we're not aware of any candidacies for this politician."""
        try:
            return self.candidacy_set.order_by('-election__date').select_related('election')[0]
        except IndexError:
            return None
        
    def save(self, *args, **kwargs):
        super(Politician, self).save(*args, **kwargs)
        self.add_alternate_name(self.name)
            
    @models.permalink
    def get_absolute_url(self):
        if self.slug:
            return 'politician', [], {'pol_slug': self.slug}
        return ('politician', [], {'pol_id': self.id})

    @property
    def identifier(self):
        return self.slug if self.slug else self.id
        
    # temporary, hackish, for stupid api framework
    @property
    def url(self):
        return "http://openparliament.ca" + self.get_absolute_url()

    @property
    def parlpage(self):
        parlid = self.info().get('parl_mp_id')
        if parlid:
            return "http://www.parl.gc.ca/Parliamentarians/{}/members/{}({})".format(
                settings.LANGUAGE_CODE, self.identifier, parlid)
        return None
        
    @models.permalink
    def get_contact_url(self):
        if self.slug:
            return ('politician_contact', [], {'pol_slug': self.slug})
        return ('politician_contact', [], {'pol_id': self.id})
            
    @memoize_property
    def info(self):
        """Returns a dictionary of PoliticianInfo attributes for this politician.
        e.g. politician.info()['web_site']
        """
        return dict([i for i in self.politicianinfo_set.all().values_list('schema', 'value')])
        
    @memoize_property
    def info_multivalued(self):
        """Returns a dictionary of PoliticianInfo attributes for this politician,
        where each key is a list of items. This allows more than one value for a
        given key."""
        info = {}
        for i in self.politicianinfo_set.all().values_list('schema', 'value'):
            info.setdefault(i[0], []).append(i[1])
        return info
        
    def set_info(self, key, value, overwrite=True):
        try:
            info = self.politicianinfo_set.get(schema=key)
            if not overwrite:
                raise ValueError("Cannot overwrite key %s on %s with %s"
                    %(key, self, value))
        except PoliticianInfo.DoesNotExist:
            info = PoliticianInfo(politician=self, schema=key)
        except PoliticianInfo.MultipleObjectsReturned:
            logger.error("Multiple objects found for schema %s on politician %r: %r" %
                (key, self,
                 self.politicianinfo_set.filter(schema=key).values_list('value', flat=True)
                    ))
            self.politicianinfo_set.filter(schema=key).delete()
            info = PoliticianInfo(politician=self, schema=key)
        info.value = unicode(value)
        info.save()
        
    def set_info_multivalued(self, key, value):
        PoliticianInfo.objects.get_or_create(politician=self, schema=key, value=unicode(value))

    def del_info(self, key):
        self.politicianinfo_set.filter(schema=key).delete()

    def get_text_analysis_qs(self, debates_only=False):
        """Return a QuerySet of Statements to be used in text corpus analysis."""
        statements = self.statement_set.filter(procedural=False)
        if debates_only:
            statements = statements.filter(document__document_type='D')
        if self.current_member:
            # For current members, we limit to the last two years for better
            # comparison.
            statements = statements.filter(time__gte=datetime.datetime.now() - datetime.timedelta(weeks=100))
        return statements

    def download_headshot(self, url):
        resp = requests.get(url)
        resp.raise_for_status()
        self.headshot.save(str(self.identifier) + ".jpg", ContentFile(resp.content))
        self.save()

class PoliticianInfoManager(models.Manager):
    """Custom manager ensures we always pull in the politician FK."""
    
    def get_queryset(self):
        return super(PoliticianInfoManager, self).get_queryset()\
            .select_related('politician')

# Not necessarily a full list           
POLITICIAN_INFO_SCHEMAS = (
    'alternate_name',
    'twitter',
    'parl_id',
    'parlinfo_id',
    'freebase_id',
    'wikipedia_id'
)
            
class PoliticianInfo(models.Model):
    """Key-value store for attributes of a Politician."""
    politician = models.ForeignKey(Politician)
    schema = models.CharField(max_length=40, db_index=True)
    value = models.TextField()

    created = models.DateTimeField(blank=True, null=True, default=datetime.datetime.now)
    
    objects = models.Manager()
    sr_objects = PoliticianInfoManager()

    def __unicode__(self):
        return u"%s: %s" % (self.politician, self.schema)
        
    @property
    def int_value(self):
        return int(self.value)

class SessionManager(models.Manager):
    
    def with_bills(self):
        return self.get_queryset().filter(bill__number_only__gt=1).distinct()
    
    def current(self):
        return self.get_queryset().order_by('-start')[0]

    def get_by_date(self, date):
        return self.filter(models.Q(end__isnull=True) | models.Q(end__gte=date))\
            .get(start__lte=date)

    def get_from_string(self, string):
        """Given a string like '41st Parliament, 1st Session, returns the session."""
        match = re.search(r'^(\d\d)\D+(\d)\D', string)
        if not match:
            raise ValueError(u"Could not find parl/session in %s" % string)
        pk = match.group(1) + '-' + match.group(2)
        return self.get_queryset().get(pk=pk)

    def get_from_parl_url(self, url):
        """Given a parl.gc.ca URL with Parl= and Ses= query-string parameters,
        return the session."""
        parlnum = re.search(r'[pP]arl=(\d\d)', url).group(1)
        sessnum = re.search(r'(?:session|Ses)=(\d)', url).group(1)
        pk = parlnum + '-' + sessnum
        return self.get_queryset().get(pk=pk)

class Session(models.Model):
    "A session of Parliament."
    
    id = models.CharField(max_length=4, primary_key=True)
    name = models.CharField(max_length=100)
    start = models.DateField()
    end = models.DateField(blank=True, null=True)
    parliamentnum = models.IntegerField(blank=True, null=True)
    sessnum = models.IntegerField(blank=True, null=True)

    objects = SessionManager()
    
    class Meta:
        ordering = ('-start',)

    def __unicode__(self):
        return self.name
        
    def has_votes(self):
        return bool(self.votequestion_set.all().count())
    
class RidingManager(models.Manager):
    
    # FIXME: This should really be in the database, not the model
    FIX_RIDING = {
        'richmond-arthabasca': 'richmond-arthabaska',
        'richemond-arthabaska': 'richmond-arthabaska',
        'battle-river': 'westlock-st-paul',
        'vancouver-est': 'vancouver-east',
        'calgary-ouest': 'calgary-west',
        'kitchener-wilmot-wellesley-woolwich': 'kitchener-conestoga',
        'carleton-orleans': 'ottawa-orleans',
        'frazer-valley-west': 'fraser-valley-west',
        'laval-ouest': 'laval-west',
        'medecine-hat': 'medicine-hat',
        'lac-st-jean': 'lac-saint-jean',
        'vancouver-north': 'north-vancouver',
        'laval-est': 'laval-east',
        'ottawa-ouest-nepean': 'ottawa-west-nepean',
        'cap-breton-highlands-canso': 'cape-breton-highlands-canso',
        'winnipeg-centre-sud': 'winnipeg-south-centre',
        'renfrew-nippissing-pembroke': 'renfrew-nipissing-pembroke',
        'the-battleford-meadow-lake': 'the-battlefords-meadow-lake',
        'esquimalt-de-fuca': 'esquimalt-juan-de-fuca',
        'sint-hubert': 'saint-hubert',
        #'edmonton-mill-woods-beaumont': 'edmonton-beaumont',
    }
    
    def get_by_name(self, name):
        slug = parsetools.slugify(name)
        if slug in RidingManager.FIX_RIDING:
            slug = RidingManager.FIX_RIDING[slug]
        return self.get_queryset().get(slug=slug)

if settings.LANGUAGE_CODE.startswith('fr'):
    PROVINCE_CHOICES = (
        ('AB', 'Alberta'),
        ('BC', 'C.-B.'),
        ('SK', 'Saskatchewan'),
        ('MB', 'Manitoba'),
        ('ON', 'Ontario'),
        ('QC', 'Québec'),
        ('NB', 'Nouveau-Brunswick'),
        ('NS', 'Nouvelle-Écosse'),
        ('PE', 'Île-du-Prince-Édouard'),
        ('NL', 'Terre-Neuve & Labrador'),
        ('YT', 'Yukon'),
        ('NT', 'Territories du Nord-Ouest'),
        ('NU', 'Nunavut'),
    )
else:
    PROVINCE_CHOICES = (
        ('AB', 'Alberta'),
        ('BC', 'B.C.'),
        ('SK', 'Saskatchewan'),
        ('MB', 'Manitoba'),
        ('ON', 'Ontario'),
        ('QC', 'Québec'),
        ('NB', 'New Brunswick'),
        ('NS', 'Nova Scotia'),
        ('PE', 'P.E.I.'),
        ('NL', 'Newfoundland & Labrador'),
        ('YT', 'Yukon'),
        ('NT', 'Northwest Territories'),
        ('NU', 'Nunavut'),
    )
PROVINCE_LOOKUP = dict(PROVINCE_CHOICES)

class Riding(models.Model):
    "A federal riding."
    
    name_en = models.CharField(max_length=200)
    name_fr = models.CharField(blank=True, max_length=200)
    province = models.CharField(max_length=2, choices=PROVINCE_CHOICES)
    slug = models.CharField(max_length=60, unique=True, db_index=True)
    edid = models.IntegerField(blank=True, null=True, db_index=True)
    current = models.BooleanField(blank=True, default=False)
    
    objects = RidingManager()

    name = language_property('name')
    
    class Meta:
        ordering = ('province', 'name_en')
        
    def save(self):
        if not self.slug:
            self.slug = parsetools.slugify(self.name_en)
        super(Riding, self).save()
        
    @property
    def dashed_name(self):
        return self.name.replace('--', u'—')
        
    def __unicode__(self):
        return u"%s (%s)" % (self.dashed_name, self.get_province_display())
        
class ElectedMemberManager(models.Manager):
    
    def current(self):
        return self.get_queryset().filter(end_date__isnull=True)
        
    def former(self):
        return self.get_queryset().filter(end_date__isnull=False)
    
    def on_date(self, date):
        return self.get_queryset().filter(models.Q(start_date__lte=date)
            & (models.Q(end_date__isnull=True) | models.Q(end_date__gte=date)))
    
    def get_by_pol(self, politician, date=None, session=None):
        if not date and not session:
            raise Exception("Provide either a date or a session to get_by_pol.")
        if date:
            return self.on_date(date).get(politician=politician)
        else:
            # In the case of floor crossers, there may be more than one ElectedMember
            # We haven't been given a date, so just return the first EM
            qs = self.get_queryset().filter(politician=politician, sessions=session).order_by('-start_date')
            if not len(qs):
                raise ElectedMember.DoesNotExist("No elected member for %s, session %s" % (politician, session))
            return qs[0]
    
class ElectedMember(models.Model):
    """Represents one person, elected to a given riding for a given party."""
    sessions = models.ManyToManyField(Session)
    politician = models.ForeignKey(Politician)
    riding = models.ForeignKey(Riding)
    party = models.ForeignKey(Party)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(blank=True, null=True, db_index=True)
    
    objects = ElectedMemberManager()
    
    def __unicode__ (self):
        if self.end_date:
            return u"%s (%s) was the member from %s from %s to %s" % (self.politician, self.party, self.riding, self.start_date, self.end_date)
        else:
            return u"%s (%s) is the member from %s (since %s)" % (self.politician, self.party, self.riding, self.start_date)

    def to_api_dict(self, representation, include_politician=True):
        d = dict(
            url=self.get_absolute_url(),
            start_date=unicode(self.start_date),
            end_date=unicode(self.end_date) if self.end_date else None,
            party={
                'name': {'en':self.party.name_en},
                'short_name': {'en':self.party.short_name_en}
            },
            label={'en': u"%s MP for %s" % (self.party.short_name, self.riding.dashed_name)},
            riding={
                'name': {'en': self.riding.dashed_name},
                'province': self.riding.province,
                'id': self.riding.edid,
            }
        )
        if include_politician:
            d['politician_url'] = self.politician.get_absolute_url()
        return d

    def get_absolute_url(self):
        return urlresolvers.reverse('politician_membership', kwargs={'member_id': self.id})
            
    @property
    def current(self):
        return not bool(self.end_date)
        
class SiteNews(models.Model):
    """Entries for the semi-blog on the openparliament homepage."""
    date = models.DateTimeField(default=datetime.datetime.now)
    title = models.CharField(max_length=200)
    text = models.TextField()
    active = models.BooleanField(default=True)
    
    objects = models.Manager()
    public = ActiveManager()

    def html(self):
        return mark_safe(markdown(self.text))
    
    class Meta:
        ordering = ('-date',)

