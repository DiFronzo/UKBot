#encoding=utf-8
from __future__ import unicode_literals
import matplotlib
matplotlib.use('svg')

import numpy as np
import time
from datetime import datetime, timedelta
import pytz
import re
import sqlite3
from odict import odict
import mwclient
import urllib
import argparse 
import codecs

from danmicholoparser import DanmicholoParser, DanmicholoParseError
import ukcommon
from ukcommon import log
from ukrules import *
from ukfilters import *

import locale
locale.setlocale(locale.LC_TIME, 'no_NO.utf-8'.encode('utf-8'))

rosettfiler = {
    'blå': 'Article blue.svg',
    'rød': 'Article red.svg',
    'oransj': 'Article orange.svg',
    'orange': 'Article orange.svg',
    'grønn': 'Article green.svg',
    'grå': 'Article grey.svg',
    'lyslilla': 'Article MediumPurple.svg',
    'lilla': 'Article purple.svg',
    'brun': 'Article brown.svg',
    'gul': 'Article yellow.svg'
}

# Suggested crontab:
## Oppdater resultater annenhver time mellom kl 8 og 22 samt kl 23 og 01...
#0 8-22/2,23,1 * * * nice -n 11 /uio/arkimedes/s01/dmheggo/wikipedia/UKBot/bot.sh
## ... og ved midnatt tirsdag til søndag (2-6,0)
#0 0 * * 2-6,0 nice -n 11 /uio/arkimedes/s01/dmheggo/wikipedia/UKBot/bot.sh
## Midnatt natt til mandag avslutter vi konkurransen
#0 0 * * 1 nice -n 11 /uio/arkimedes/s01/dmheggo/wikipedia/UKBot/ended.sh
## og så sjekker vi om det er klart for å sende ut resultater
#20 8-23/1 * * 1-2 nice -n 11 /uio/arkimedes/s01/dmheggo/wikipedia/UKBot/close.sh
#20 */12 * * 3-6 nice -n 11 /uio/arkimedes/s01/dmheggo/wikipedia/UKBot/close.sh
## Hver natt kl 00.30 laster vi opp ny figur
#30 0 * * * nice -n 11 /uio/arkimedes/s01/dmheggo/wikipedia/UKBot/uploadbot.sh

#CREATE TABLE contests (
#  name TEXT,
#  ended INTEGER NOT NULL,
#  closed INTEGER NOT NULL,
#  PRIMARY KEY(name)  
#);
#CREATE TABLE contribs (
#  revid INTEGER NOT NULL,
#  site TEXT NOT NULL,
#  parentid INTEGER NOT NULL,
#  user TEXT NOT NULL, 
#  page TEXT NOT NULL, 
#  timestamp DATETIME NOT NULL, 
#  size  INTEGER NOT NULL,
#  parentsize  INTEGER NOT NULL,
#  PRIMARY KEY(revid, site)
#);
#CREATE TABLE fulltexts (
#  revid INTEGER NOT NULL,
#  site TEXT NOT NULL,
#  revtxt TEXT NOT NULL,
#  PRIMARY KEY(revid, site)  
#);
#CREATE TABLE users (
#  contest TEXT NOT NULL,
#  user TEXT NOT NULL,
#  week INTEGER NOT NULL,
#  points REAL NOT NULL,
#  bytes INTEGER NOT NULL,
#  newpages INTEGER NOT NULL,
#  week2 INTEGER NOT NULL,
#  PRIMARY KEY(contest, user)  
#);


#from ete2 import Tree

#from progressbar import ProgressBar, Counter, Timer, SimpleProgress
#pbar = ProgressBar(widgets = ['Processed: ', Counter(), ' revisions (', Timer(), ')']).start()
#pbar.maxval = pbar.currval + 1
#pbar.update(pbar.currval+1)
#pbar.finish()


class ParseError(Exception):
    """Raised when wikitext input is not on the expected form, so we don't find what we're looking for"""
    
    def __init__(self, msg):
        self.msg = msg

class Site(mwclient.Site):

    def __init__(self, name):

        self.errors = []
        self.name = name
        self.key = name.split('.')[0]
        mwclient.Site.__init__(self, name)

class Article(object):
    
    def __init__(self, site, user, name):
        """
        An article is uniquely identified by its name and its site
        """
        self.site = site
        self.user = user
        #self.site_key = site.host.split('.')[0]
        self.name = name
        self.disqualified = False
        
        self.revisions = odict()
        self.redirect = False
        self.errors = []
    
    def __repr__(self):
        return ("<Article %s:%s for user %s>" % (self.site.key, self.name, self.user.name)).encode('utf-8')

    @property
    def new(self):
        return self.revisions[self.revisions.firstkey()].new

    def add_revision(self, revid, **kwargs):
        self.revisions[revid] = Revision(self, revid, **kwargs)
        return self.revisions[revid]
    
    @property
    def bytes(self):
        return np.sum([rev.bytes for rev in self.revisions.itervalues()])
    
    @property
    def words(self):
        return np.sum([rev.words for rev in self.revisions.itervalues()])

    @property
    def points(self):
        """ The article score is the sum of the score for its revisions, independent of whether the article is disqualified or not """
        return np.sum([rev.get_points() for rev in self.revisions.values()])

    def get_points(self, ptype = '', ignore_max = False, include_suspension_period = True):
        p = 0.
        for revid, rev in self.revisions.iteritems():
            dt = pytz.utc.localize(datetime.fromtimestamp(rev.timestamp))
            if include_suspension_period == True or self.user.suspended_since == None or dt < self.user.suspended_since:
                p += rev.get_points(ptype, ignore_max)
            else:
                if self.user.contest.verbose:
                    log('!! Skipping revision %d in suspension period' % revid)
        return p
        #return np.sum([a.points for a in self.articles.values()])


class Revision(object):
    
    def __init__(self, article, revid, **kwargs):
        """
        A revision is uniquely identified by its revision id and its site

        Arguments:
          - article: (Article) article object reference
          - revid: (int) revision id
        """
        self.article = article
        self.errors = []

        self.revid = revid
        self.size = -1
        self.text = ''

        self.parentid = 0
        self.parentsize = 0
        self.parenttext = ''

        self.points = []
        
        for k, v in kwargs.iteritems():
            if k == 'timestamp':
                self.timestamp = int(v)
            elif k == 'parentid':
                self.parentid = int(v)
            elif k == 'size':
                self.size = int(v)
            elif k == 'parentsize':
                self.parentsize = int(v)
            else:
                raise StandardError('add_revision got unknown argument %s' % k)
    
    def __repr__(self):
        return ("<Revision %d for %s:%s>" % (self.revid, self.site.key, self.article.name)).encode('utf-8')

    @property
    def bytes(self):
        return self.size - self.parentsize

    @property
    def words(self):
        try:
            return self._wordcount
        except:
            dp = DanmicholoParser(self.text)
            dp2 = DanmicholoParser(self.parenttext)
            try:
                self._wordcount = len(dp.maintext.split()) - len(dp2.maintext.split())
                if len(dp.parse_errors) > 0:
                    self.errors.append('Et problem med revisjon %d kan ha påvirket ordtellingen for denne: <nowiki>%s</nowiki> ' % (self.revid, dp.parse_errors[0]))
                if len(dp2.parse_errors) > 0:
                    self.errors.append('Et problem med revisjon %d kan ha påvirket ordtellingen for denne: <nowiki>%s</nowiki> ' % (self.parentid, dp2.parse_errors[0]))
            except DanmicholoParseError as e:
                log("!!!>> FAIL: %s @ %d" % (self.article.name,self.revid))
                self._wordcount = 0
                #raise
            return self._wordcount

    @property
    def new(self):
        return (self.parentid == 0)

    @property
    def redirect(self):
        return bool(re.match(r'#(OMDIRIGERING|REDIRECT)', self.text))

    @property
    def parentredirect(self):
        return bool(re.match(r'#(OMDIRIGERING|REDIRECT)', self.parenttext))
    
    def get_link(self):
        """ returns a link to revision """
        q = { 'title': self.article.name.encode('utf-8'), 'oldid': self.revid }
        if not self.new:
            q['diff'] = 'prev'
        return '//' + self.article.site.host + self.article.site.site['script'] + '?' + urllib.urlencode(q)
    
    def get_parent_link(self):
        """ returns a link to parent revision """
        q = { 'title': self.article.name.encode('utf-8'), 'oldid': self.parentid }
        return '//' + self.article.site.host + self.article.site.site['script'] + '?' + urllib.urlencode(q)
    
    def get_points(self, ptype = '', ignore_max = False):
        p = 0.0
        for pnt in self.points:
            if ptype == '' or pnt[1] == ptype:
                if ignore_max and len(pnt) > 3:
                    p += pnt[3]
                else:
                    p += pnt[0]
        return p


class User(object):

    def __init__(self, username, contest):
        self.name = username
        self.articles = odict()
        self.contest = contest
        self.suspended_since = None
        self.disqualified_articles = []

    def __repr__(self):
        return ("<User %s>" % self.name).encode('utf-8')
    
    @property
    def revisions(self):
        # oh my, funny (and fast) one-liner for making a flat list of revisions
        return { rev.revid : rev for article in self.articles.values() for rev in article.revisions.values() }

    def sort_contribs(self):

        # sort revisions by revision id
        for article in self.articles.itervalues():
            article.revisions.sort( key = lambda x: x[0] ) # sort by key (revision id)

        # sort articles by first revision id
        self.articles.sort( key = lambda x: x[1].revisions.firstkey() )
    
    def add_contribs_from_wiki(self, site, start, end, fulltext = False, namespace = 0):
        """
        Populates self.articles with entries from the API.

            site      : mwclient.client.Site object
            start     : datetime object with timezone Europe/Oslo
            end       : datetime object with timezone Europe/Oslo
            fulltext  : get revision fulltexts
            namespace : namespace ID
        """
        apilim = 50
        if 'bot' in site.rights:
            apilim = site.api_limit         # API limit, should be 500

        site_key = site.host.split('.')[0]
        
        ts_start = start.astimezone(pytz.utc).strftime('%FT%TZ')
        ts_end = end.astimezone(pytz.utc).strftime('%FT%TZ')

        # 1) Fetch user contributions

        new_articles = []
        new_revisions = []
        for c in site.usercontributions(self.name, ts_start, ts_end, 'newer', prop = 'ids|title|timestamp|comment', namespace = namespace ):
            #pageid = c['pageid']
            article_comment = c['comment']
            if not article_comment[:13] == 'Tilbakestilte':
                rev_id = c['revid']
                article_title = c['title']
                article_key = site_key + ':' + article_title
                
                if not rev_id in self.revisions:

                    if not article_key in self.articles:
                        self.articles[article_key] = Article(site, self, article_title)
                        if article_key in self.disqualified_articles:
                            self.articles[article_key].disqualified = True

                        new_articles.append(self.articles[article_key])
                
                    article = self.articles[article_key]
                
                    # We check self.revisions instead of article.revisions, because the revision may
                    # already belong to "another article" (another title) if the article has been moved
                    rev = article.add_revision(rev_id, timestamp = time.mktime(c['timestamp']) )
                    new_revisions.append(rev)
            
        # Always sort after we've added contribs
        self.sort_contribs()
        if len(new_revisions) > 0 or len(new_articles) > 0:
            log(" -> [%s] Added %d new revisions, %d new articles from API" % (site_key, len(new_revisions), len(new_articles)))

        # 2) Check if pages are redirects (this information can not be cached, because other users may make the page a redirect)
        #    If we fail to notice a redirect, the contributions to the page will be double-counted, so lets check

        titles = [a.name for a in self.articles.values() if a.site.key == site_key]
        for s0 in range(0, len(titles), apilim):
            ids = '|'.join(titles[s0:s0+apilim])
            for page in site.api('query', prop = 'info', titles = ids)['query']['pages'].itervalues():
                article_key = site_key + ':' + page['title']
                self.articles[article_key].redirect = ('redirect' in page.keys())

        # 3) Fetch info about the new revisions: diff size, possibly content

        props = 'ids|size'
        if fulltext:
            props += '|content'
        revids = [str(r.revid) for r in new_revisions]
        parentids = []
        nr = 0
        for s0 in range(0, len(new_revisions), apilim):
            #print "API limit is ",apilim," getting ",s0
            ids = '|'.join(revids[s0:s0+apilim])
            for page in site.api('query', prop = 'revisions', rvprop = props, revids = ids)['query']['pages'].itervalues():
                article_key = site_key + ':' + page['title']
                for apirev in page['revisions']:
                    nr +=1
                    rev = self.articles[article_key].revisions[apirev['revid']]
                    rev.parentid = apirev['parentid']
                    rev.size = apirev['size']
                    if '*' in apirev.keys():
                        rev.text = apirev['*']
                    if not rev.new:
                        parentids.append(rev.parentid)
        if nr > 0:
            log(" -> [%s] Checked %d of %d revisions, found %d parent revisions" % (site_key, nr, len(new_revisions), len(parentids)))

        if nr != len(new_revisions):
            raise StandardError("Did not get all revisions")
        
        # 4) Fetch info about the parent revisions: diff size, possibly content
        
        props = 'ids|size'
        if fulltext:
            props += '|content'
        nr = 0
        parentids = [str(i) for i in parentids]
        for s0 in range(0, len(parentids), apilim):
            ids = '|'.join(parentids[s0:s0+apilim])
            for page in site.api('query', prop = 'revisions', rvprop = props, revids = ids)['query']['pages'].itervalues():
                article_key = site_key + ':' + page['title']
                article = self.articles[article_key]
                for apirev in page['revisions']:
                    nr +=1
                    parentid = apirev['revid']
                    found = False
                    for revid, rev in article.revisions.iteritems():
                        if rev.parentid == parentid:
                            found = True
                            break
                    if not found:
                        raise StandardError("No revision found matching title=%s, parentid=%d" % (page['title'], parentid))

                    rev.parentsize = apirev['size']
                    if '*' in apirev.keys():
                        rev.parenttext = apirev['*']
        if nr > 0:
            log(" -> [%s] Checked %d parent revisions" % (site_key, nr))

    
    def save_contribs_to_db(self, sql):
        """ Save self.articles to DB so it can be read by add_contribs_from_db """

        cur = sql.cursor()
        nrevs = 0
        ntexts = 0

        for article_key, article in self.articles.iteritems():
            site_key = article.site.key

            for revid, rev in article.revisions.iteritems():
                ts = datetime.fromtimestamp(rev.timestamp).strftime('%F %T')
                
                # Save revision if not already saved
                if len( cur.execute(u'SELECT revid FROM contribs WHERE revid=? AND site=?', [revid, site_key]).fetchall() ) == 0:
                    cur.execute(u'INSERT INTO contribs (revid, site, parentid, user, page, timestamp, size, parentsize) VALUES (?,?,?,?,?,?,?,?)', 
                        (revid, site_key, rev.parentid, self.name, article.name, ts, rev.size, rev.parentsize))
                    nrevs += 1

                # Save revision text if we have it and if not already saved
                if len(rev.text) > 0 and len( cur.execute(u'SELECT revid FROM fulltexts WHERE revid=? AND site=?', [revid, site_key]).fetchall() ) == 0:
                    cur.execute(u'INSERT INTO fulltexts (revid, site, revtxt) VALUES (?,?,?)', (revid, site_key, rev.text) )
                    ntexts += 1

                # Save parent revision text if we have it and if not already saved
                if len(rev.parenttext) > 0 and len( cur.execute(u'SELECT revid FROM fulltexts WHERE revid=? AND site=?', [rev.parentid, site_key]).fetchall() ) == 0:
                    cur.execute(u'INSERT INTO fulltexts (revid, site, revtxt) VALUES (?,?,?)', (rev.parentid, site_key, rev.parenttext) )
                    ntexts += 1

        sql.commit()
        cur.close()
        if nrevs > 0 or ntexts > 0:
            log(" -> Wrote %d revisions and %d fulltexts to DB" % (nrevs, ntexts))
    
    def add_contribs_from_db(self, sql, start, end, sites):
        """
        Populates self.articles with entries from SQLite DB

            sql   : sqlite3.Connection object
            start : datetime object
            end   : datetime object
        """
        cur = sql.cursor()
        cur2 = sql.cursor()
        ts_start = start.astimezone(pytz.utc).strftime('%F %T')
        ts_end = end.astimezone(pytz.utc).strftime('%F %T')
        nrevs = 0
        narts = 0
        for row in cur.execute(u"""SELECT revid, site, parentid, page, timestamp, size, parentsize FROM contribs 
                WHERE user=? AND timestamp >= ? AND timestamp <= ?""", (self.name, ts_start, ts_end)):

            rev_id, site_key, parent_id, article_title, ts, size, parentsize = row
            article_key = site_key + ':' + article_title
            ts = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S').strftime('%s')

            # Add article if not present
            if not article_key in self.articles:
                narts +=1
                self.articles[article_key] = Article(sites[site_key], self, article_title) 
                if article_key in self.disqualified_articles:
                    self.articles[article_key].disqualified = True
            article = self.articles[article_key]
            
            # Add revision if not present
            if not rev_id in self.revisions:
                nrevs += 1
                article.add_revision(rev_id, timestamp = ts, parentid = parent_id, size = size, parentsize = parentsize)
            rev = self.revisions[rev_id]

            # Add revision text
            for row2 in cur2.execute(u"""SELECT revtxt FROM fulltexts WHERE revid=? AND site=?""", [rev_id, site_key]):
                rev.text = row2[0]
            
            # Add parent revision text
            if not rev.new:
                for row2 in cur2.execute(u"""SELECT revtxt FROM fulltexts WHERE revid=? AND site=?""", [parent_id, site_key]):
                    rev.parenttext = row2[0]

        cur.close()
        cur2.close()

        # Always sort after we've added contribs
        self.sort_contribs()

        if nrevs > 0 or narts > 0:
            log(" -> Added %d revisions, %d articles from DB" % (nrevs, narts))

    def filter(self, filters):

        for filter in filters:
            if self.contest.verbose:
                log('>> Before %s (%d) : %s' % (type(filter).__name__, len(self.articles), ', '.join(self.articles.keys())))

            self.articles = filter.filter(self.articles)
            
            if self.contest.verbose:
                log('>> After %s (%d) : %s' % (type(filter).__name__, len(self.articles), ', '.join(self.articles.keys())))

        # We should re-sort afterwards since not all filters preserve the order (notably the CatFilter)
        self.sort_contribs()

        log(" -> %d articles remain after filtering" % len(self.articles))
        if self.contest.verbose:
            log('----')
            for a in self.articles.iterkeys():
                log('%s' % a)
            log('----')

    @property
    def bytes(self):
        return np.sum([a.bytes for a in self.articles.itervalues()])
    
    @property
    def newpages(self):
        return np.sum([1 for a in self.articles.itervalues() if a.new and not a.redirect])
    
    @property
    def words(self):
        return np.sum([a.words for a in self.articles.itervalues()])
    
    @property
    def points(self):
        """ The points for all the user's articles, excluding disqualified ones """
        p = 0.
        for article_key, article in self.articles.iteritems():
            if not article_key in self.disqualified_articles:
                for revid, rev in article.revisions.iteritems():
                    dt = pytz.utc.localize(datetime.fromtimestamp(rev.timestamp))
                    if self.suspended_since == None or dt < self.suspended_since:
                        p += rev.get_points()
        return p
        #return np.sum([a.points for a in self.articles.values()])

    def analyze(self, rules):

        x = []
        y = []
        utc = pytz.utc
        osl = pytz.timezone('Europe/Oslo')
        
        # loop over articles
        for article_key, article in self.articles.iteritems():
            log('.', newline = False)
            
            # loop over revisions
            for revid, rev in article.revisions.iteritems():

                rev.points = []

                # loop over rules
                for rule in rules:
                    rule.test(rev)

                if not article.disqualified:

                    dt = pytz.utc.localize(datetime.fromtimestamp(rev.timestamp))
                    if self.suspended_since == None or dt < self.suspended_since:

                        if rev.get_points() > 0:
                            #print self.name, rev.timestamp, rev.get_points()
                            ts = float(utc.localize(datetime.fromtimestamp(rev.timestamp)).astimezone(osl).strftime('%s'))
                            x.append(ts)
                            y.append(float(rev.get_points()))

        x = np.array(x)
        y = np.array(y)

        o = np.argsort(x)
        x = x[o]
        y = y[o]
        #pl = np.array(pl, dtype=float)
        #pl.sort(axis = 0)
        y2 = np.array([np.sum(y[:q+1]) for q in range(len(y))])
        self.plotdata = np.column_stack((x,y2))
        #np.savetxt('user-%s'%self.name, np.column_stack((x,y,y2)))
        

    def format_result(self, pos = -1, closing = False, prices= []):
        
        entries = []

        utc = pytz.utc
        osl = pytz.timezone('Europe/Oslo')

        if self.contest.verbose:
            log('Formatting results for user %s' % self.name)
        # loop over articles
        for article_key, article in self.articles.iteritems():
            
            if article.points == 0.0:

                if self.contest.verbose:
                    log('    %s: skipped (0 points)' % article_key)

            else:

                # loop over revisions
                revs = []
                for revid, rev in article.revisions.iteritems():

                    if len(rev.points) > 0:
                        descr = ' + '.join(['%.1f p (%s)' % (p[0], p[2]) for p in rev.points])
                        dt = utc.localize(datetime.fromtimestamp(rev.timestamp))
                        dt_str = dt.astimezone(osl).strftime('%A, %H:%M').decode('utf-8')
                        out = '[%s %s]: %s' % (rev.get_link(), dt_str, descr)
                        if self.suspended_since != None and dt > self.suspended_since:
                            out = '<s>' + out + '</s>'
                        if len(rev.errors) > 0:
                            out = '[[File:Ambox warning yellow.svg|12px|%s]] ' % (', '.join(rev.errors)) + out
                        revs.append(out)
                
                titletxt = ''
                try:
                    titletxt = 'Kategoritreff: ' + ' &gt; '.join(article.cat_path) + '<br />'
                except AttributeError:
                    pass
                titletxt += '<br />'.join(revs)
                titletxt += '<div style="border-top:1px solid #CCC">Totalt {{formatnum:%d}} bytes, %d ord.</div>' % (article.bytes, article.words)
                
                ap = article.points
                if article_key in self.disqualified_articles:
                    cp = 0.
                else:
                    cp = article.get_points(include_suspension_period = False)

                p = '%.1f p' % ap
                if ap != cp:
                    p = '<s>'+p+'</s>'
                    if cp != 0.:
                        p += '%.1f p' % cp

                out = '[[:%s|%s]]' % (article_key, article.name)
                if article_key in self.disqualified_articles:
                    out = '[[Fil:Qsicon Achtung.png|14px]] <s>' + out + '</s>'
                    titletxt += '<div style="border-top:1px solid red; background:#ffcccc;"><strong>Merk:</strong> Bidragene til artikkelen fra denne brukeren er diskvalifisert og teller ikke i konkurransen</div>'
                elif cp != ap:
                    out = '[[Fil:Qsicon Achtung.png|14px]] ' + out
                    titletxt += '<div style="border-top:1px solid red; background:#ffcccc;"><strong>Merk:</strong> En eller flere revisjoner er ikke talt med fordi de ble gjort mens brukeren var suspendert. Hvis suspenderingen oppheves vil bidragene telle med.</div>'
                out += ' (<abbr class="uk-ap">%s</abbr>)' % p

                out = '# ' + out
                out += '<div class="uk-ap-title" style="font-size: smaller; color:#888; line-height:100%;">' + titletxt + '</div>'
                
                entries.append(out)
                if self.contest.verbose:
                    log('    %s: %.f / %.f points' % (article_key, cp, ap) , newline = False)
                    log('    -- %.f / %.f points' % (article.get_points(include_suspension_period = False), article.get_points(include_suspension_period = True)))

        ros = ''
        if closing:
            if pos == 0:
                for r in prices:
                    if r[1] == 'winner':
                        ros += '[[Fil:%s|20px]] ' % rosettfiler[r[0]]
                        break
            for r in prices:
                if r[1] == 'pointlimit' and self.points >= r[2]:
                    ros += '[[Fil:%s|20px]] ' % rosettfiler[r[0]]
                    break
        suspended = ''
        if self.suspended_since != None:
            suspended = ', suspendert siden %s' % self.suspended_since.strftime('%A, %H.%M').decode('utf-8')
        out = '=== %s [[Bruker:%s|%s]] (%.f p%s) ===\n' % (ros, self.name, self.name, self.points, suspended)
        if len(entries) == 0:
            out += "''Ingen kvalifiserte bidrag registrert ennå''"
        else:
            out += '%d artikler, {{formatnum:%.2f}} kB\n' % (len(entries), self.bytes/1000.)
        if len(entries) > 10:
            out += '{{Kolonner}}\n'
        out += '\n'.join(entries)
        out += '\n\n'

        return out


class UK(object):

    def __init__(self, page, catignore, sites, sql, verbose = False):
        """
            page: mwclient.Page object
            catignore: string
            sites: list
            sql: sqlite3 object
            verbose: boolean
        """
        self.page = page
        self.name = self.page.name
        txt = page.edit(readonly = True)
        m = re.search('==\s*Resultater\s*==',txt)
        txt = txt[:m.end()]

        self.verbose = verbose
        self.sql = sql
        sections = [s.strip() for s in re.findall('^[\s]*==([^=]+)==', txt, flags = re.M)]
        self.results_section = sections.index('Resultater') + 1

        self.sites = sites
        self.users = [User(n, self) for n in self.extract_userlist(txt)]
        self.rules, self.filters = self.extract_rules(txt, catignore)
        
        if self.startweek == self.endweek:
            log('@ Uke %d' % self.startweek)
        else:
            log('@ Uke %d–%d' % (self.startweek, self.endweek))

    def extract_userlist(self, txt):
        lst = []
        m = re.search('==\s*Delta[kg]ere\s*==',txt)
        if not m:
            raise ParseError('Fant ikke deltakerlisten!')
        deltakerliste = txt[m.end():]
        m = re.search('==[^=]+==',deltakerliste)
        if not m:
            raise ParseError('Fant ingen overskrift etter deltakerlisten!')
        deltakerliste = deltakerliste[:m.start()]
        for d in deltakerliste.split('\n'):
            q = re.search(r'\[\[([^:]+):([^|\]]+)', d)
            if q:
                lst.append(q.group(2))
        log("@ Fant %d deltakere" % (len(lst)))
        return lst


    def extract_rules(self, txt, catignore_txt):
        rules = []
        filters = []

        dp = DanmicholoParser(txt)
        dp2 = DanmicholoParser(catignore_txt)
        
        if not 'ukens konkurranse poeng' in dp.templates.keys():
            raise ParseError('Denne konkurransen har ingen poengregler. Poengregler defineres med {{tl|ukens konkurranse poeng}}.')
        
        #if not 'ukens konkurranse kriterium' in dp.templates.keys():
        #    raise ParseError('Denne konkurransen har ingen bidragskriterier. Kriterier defineres med {{tl|ukens konkurranse kriterium}}.')
        
        if not 'infoboks ukens konkurranse' in dp.templates.keys():
            raise ParseError('Denne konkurransen mangler en {{tl|infoboks ukens konkurranse}}-mal.')

        try:
            catignore = dp2.tags['pre'][0]['content'].splitlines()
        except (IndexError, KeyError):
            raise ParseError('Klarte ikke tolke catignore-siden')

        # Read filters

        nfilters = 0
        #print dp.templates.keys()
        if 'ukens konkurranse kriterium' in dp.templates.keys():
            for templ in dp.templates['ukens konkurranse kriterium']:
                nfilters += 1
                p = templ.parameters
                anon = [[k,p[k]] for k in p.keys() if type(k) == int]
                anon = sorted(anon, key = lambda x: x[0])
                anon = [a[1] for a in anon]
                named = [[k,p[k]] for k in p.keys() if type(k) != int]

                named = odict(named)
                key = anon[0].lower()

                params = { 'verbose': self.verbose }
                if key == 'ny':
                    filters.append(NewPageFilter(**params))

                elif key == 'eksisterende':
                    filters.append(ExistingPageFilter(**params))

                elif key == 'stubb':
                    filters.append(StubFilter(**params))
                
                elif key == 'bytes':
                    if len(anon) < 2:
                        raise ParseError('Ingen bytesgrense (andre argument) ble gitt til {{mlp|ukens konkurranse kriterium|bytes}}')
                    params['bytelimit'] = anon[1]
                    filters.append(ByteFilter(**params))

                elif key == 'kategori':
                    if len(anon) < 2:
                        raise ParseError('Ingen kategori(er) ble gitt til {{mlp|ukens konkurranse kriterium|kategori}}')
                    params['sites'] = self.sites
                    params['catnames'] = anon[1:]
                    params['ignore'] = catignore
                    if 'maksdybde' in named:
                        params['maxdepth'] = int(named['maksdybde'])
                    filters.append(CatFilter(**params))

                elif key == 'tilbakelenke':
                    params['sites'] = self.sites
                    params['articles'] = anon[1:]
                    filters.append(BackLinkFilter(**params))
                
                elif key == 'fremlenke':
                    params['sites'] = self.sites
                    params['articles'] = anon[1:]
                    filters.append(ForwardLinkFilter(**params))

                else: 
                    raise ParseError('Ukjent argument gitt til {{ml|ukens konkurranse kriterium}}: '+key)
        log("@ Fant %d filtre" % (nfilters))

        # Read rules

        for templ in dp.templates['ukens konkurranse poeng']:
            p = templ.parameters
            anon = [[k,p[k]] for k in p.keys() if type(k) == int]
            anon = sorted(anon, key = lambda x: x[0])
            anon = [a[1] for a in anon]
            named = [[k,p[k]] for k in p.keys() if type(k) != int]

            named = odict(named)
            key = anon[0].lower()

            if key == 'ny':
                rules.append(NewPageRule(anon[1]))

            elif key == 'kvalifisert':
                rules.append(QualiRule(anon[1]))
            
            elif key == 'stubb':
                rules.append(StubRule(anon[1]))

            elif key == 'byte':
                params = { 'points': anon[1] }
                if 'makspoeng' in named:
                    params['maxpoints'] = named['makspoeng']
                rules.append(ByteRule(**params))

            elif key == 'ord':
                params = { 'points': anon[1] }
                if 'makspoeng' in named:
                    params['maxpoints'] = named['makspoeng']
                rules.append(WordRule(**params))

            elif key == 'bilde':
                params = { 'points': anon[1] }
                if 'makspoeng' in named:
                    params['maxpoints'] = named['makspoeng']
                rules.append(ImageRule(**params))
            
            elif key == 'ref':
                params = { 'sourcepoints': anon[1], 'refpoints': anon[2] }
                rules.append(RefRule(**params))
            
            elif key == 'malfjerning':
                params = { 'points': anon[1], 'template': anon[2] }
                if 'alias' in named:
                    params['aliases'] = [a.strip() for a in named['alias'].split(',')]
                rules.append(TemplateRemovalRule(**params))

            elif key == 'bytebonus':
                rules.append(ByteBonusRule(anon[1], anon[2]))

            elif key == 'ordbonus':
                rules.append(WordBonusRule(anon[1], anon[2]))

            else:
                raise ParseError('Ukjent argument gitt til {{ml|ukens konkurranse poeng}}: '+key)
        
        
        # Read infobox 

        try:
            infoboks = dp.templates['infoboks ukens konkurranse'][0]
        except:
            raise ParseError('Klarte ikke å tolke innholdet i {{tl|infoboks ukens konkurranse}}-malen.')
        
        utc = pytz.utc
        osl = pytz.timezone('Europe/Oslo')

        if 'år' in infoboks.parameters and 'uke' in infoboks.parameters:
            year = infoboks.parameters['år']
            startweek = infoboks.parameters['uke']
            if 'ukefler' in infoboks.parameters:
                endweek = re.sub('<\!--.+?-->', '', infoboks.parameters['ukefler']).strip()
                if endweek == '':
                    endweek = startweek
            else:
                endweek = startweek

            self.start = osl.localize(datetime.strptime(year+' '+startweek+' 1 00 00 00', '%Y %W %w %H %M %S'))
            self.end = osl.localize(datetime.strptime(year+' '+endweek+' 0 23 59 59', '%Y %W %w %H %M %S'))
        elif 'start' in infoboks.parameters and 'slutt' in infoboks.parameters:
            startdt = infoboks.parameters['start']
            enddt = infoboks.parameters['slutt']
            self.start = osl.localize(datetime.strptime(startdt + ' 00 00 00 00', '%Y-%m-%d %H %M %S'))
            self.end = osl.localize(datetime.strptime(enddt +' 23 59 59 00', '%Y-%m-%d %H %M %S'))
        else:
            raise ParseError('Fant ikke uke/år eller start/slutt i {{tl|infoboks ukens konkurranse}}.')

        self.year = self.start.isocalendar()[0]
        self.startweek = self.start.isocalendar()[1]
        self.endweek = self.end.isocalendar()[1]

        self.ledere = re.findall(r'\[\[Bruker:([^\|\]]+)', infoboks.parameters['leder'])
        if len(self.ledere) == 0:
            raise ParseError('Fant ingen konkurranseorganisatorer i {{tl|infoboks ukens konkurranse}}.')

        
        self.prices = []
        for col in rosettfiler.keys():
            if col in infoboks.parameters.keys():
                r = re.sub('<\!--.+?-->', '', infoboks.parameters[col]).strip() # strip comments, then whitespace
                if r != '':
                    r = r.split()[0].lower()
                    #print col,r
                    if r == 'vinner':
                        self.prices.append([col, 'winner', 0])
                    elif r != '':
                        try:
                            self.prices.append([col, 'pointlimit', int(r)])
                        except ValueError:
                            pass
                            #raise ParseError('Klarte ikke tolke verdien til parameteren %s gitt til {{tl|infoboks ukens konkurranse}}.' % col)

        self.prices.sort(key = lambda x: x[2], reverse = True)
        
        # Read disqualifications

        if 'uk bruker suspendert' in dp.templates:
            for templ in dp.templates['uk bruker suspendert']:
                uname = templ.parameters[1]
                try:
                    sdate = osl.localize(datetime.strptime(templ.parameters[2], '%Y-%m-%d %H:%M'))
                except ValueError:
                    raise ParseError('Klarte ikke å tolke datoen gitt til {{ml|UK bruker suspendert}}-malen.')

                #print 'Suspendert bruker:',uname,sdate
                ufound = False
                for u in self.users:
                    if u.name == uname:
                        #print " > funnet"
                        u.suspended_since = sdate
                        ufound = True
                if not ufound:
                    pass
                    # TODO: logging.warning 
                    #raise ParseError('Fant ikke brukeren %s gitt til {{ml|UK bruker suspendert}}-malen.' % uname)
        
        if 'uk bidrag diskvalifisert' in dp.templates:
            for templ in dp.templates['uk bidrag diskvalifisert']:
                uname = templ.parameters[1]
                aname = templ.parameters[2]
                #print 'Diskvalifiserte bidrag:',uname,aname
                ufound = False
                for u in self.users:
                    if u.name == uname:
                        #print " > funnet"
                        u.disqualified_articles.append(aname)
                        ufound = True
                if not ufound:
                    raise ParseError('Fant ikke brukeren %s gitt til {{ml|UK bidrag diskvalifisert}}-malen.' % uname)

        try:
            infoboks = dp.templates['infoboks ukens konkurranse'][0]
        except:
            raise ParseError('Klarte ikke å tolke innholdet i {{tl|infoboks ukens konkurranse}}-malen.')

        return rules, filters

    def plot(self):
        import matplotlib.pyplot as plt

        w = 14/2.54
        goldenratio = 1.61803399
        h = w/goldenratio
        fig = plt.figure( figsize=(w,h) )

        ax = fig.add_subplot(1,1,1, frame_on = False)
        ax.grid(True, which = 'major', color = 'gray', alpha = 0.5)
        fig.subplots_adjust(left=0.10, bottom=0.09, right=0.65, top=0.94)

        t0 = float(self.start.strftime('%s'))

        ndays = 7
        if self.startweek != self.endweek:
            ndays = 14

        xt = t0 + np.arange(ndays + 1) * 86400
        xt_mid = t0 + 43200 + np.arange(ndays) * 86400

        now = float(datetime.now().strftime('%s'))

        yall = []
        cnt = 0
        for u in self.users:
            if u.plotdata.shape[0] > 0:
                cnt += 1
                x = list(u.plotdata[:,0])
                y = list(u.plotdata[:,1])
                yall.extend(y)
                x.insert(0,xt[0])
                y.insert(0,0)
                if now < xt[-1]:
                    x.append(now)
                    y.append(y[-1])
                else:
                    x.append(xt[-1])
                    y.append(y[-1])
                l = ax.plot(x, y, linewidth=2., alpha = 0.5, label = u.name) #, markerfacecolor='#FF8C00', markeredgecolor='#888888', label = u.name)
                c = l[0].get_color()
                ax.plot(x[1:-1], y[1:-1], marker='.', markersize = 4, markerfacecolor=c, markeredgecolor=c, linewidth=0., alpha = 0.5) #, markerfacecolor='#FF8C00', markeredgecolor='#888888', label = u.name)
                if cnt >= 10:
                    break

        if now < xt[-1]:
            ax.axvline(now, color='red')

        ax.set_xticks(xt, minor = False)
        ax.set_xticklabels([], minor = False)
        
        ax.set_xticks(xt_mid, minor = True)
        if ndays == 7:
            ax.set_xticklabels(['Man','Tir','Ons','Tors','Fre','Lør','Søn'], minor = True)
        else:
            ax.set_xticklabels(['Man','','Ons','','Fre','','Søn','','Tir','','Tor','','Lør', ''], minor = True)

        for i in range(1,ndays,2):
            ax.axvspan(xt[i], xt[i+1], facecolor='#000099', linewidth=0., alpha=0.03)

        for i in range(0,ndays,2):
            ax.axvspan(xt[i], xt[i+1], facecolor='#000099', linewidth=0., alpha=0.07)

        for line in ax.xaxis.get_ticklines(minor = False):
            line.set_markersize(0)

        for line in ax.xaxis.get_ticklines(minor = True):
            line.set_markersize(0)
        
        for line in ax.yaxis.get_ticklines(minor = False):
            line.set_markersize(0)

        if len(yall) > 0:
            ax.set_xlim(t0, xt[-1])
            ax.set_ylim(0, 1.05*np.max(yall))

            plt.legend()
            ax = plt.gca()
            ax.legend( 
                # ncol = 4, loc = 3, bbox_to_anchor = (0., 1.02, 1., .102), mode = "expand", borderaxespad = 0.
                loc = 2, bbox_to_anchor = (1.0, 1.0), borderaxespad = 0., frameon = 0.
            )
            plt.savefig('Nowp Ukens konkurranse %d-%d.svg' % (self.year, self.startweek), dpi = 200)

    def deliver_prices(self):

        if self.startweek == self.endweek:
            heading = '== Ukens konkurranse uke %d == ' % self.startweek
        else:
            heading = '== Ukens konkurranse uke %d–%d == ' % (self.startweek, self.endweek)
        for i, u in enumerate(self.users):

            prizefound = False
            if i == 0:
                mld = ''
                for r in self.prices:
                    if r[1] == 'winner':
                        prizefound = True
                        if self.startweek == self.endweek:
                            mld += '{{UK vinner|visuk=nei|år=%d|uke=%d|%s=ja' % (self.year, self.startweek, r[0])
                        else:
                            mld += '{{UK vinner|visuk=nei|år=%d|uke=%d|ukefler=%d|%s=ja' % (self.year, self.startweek, self.endweek, r[0])
                        break
                for r in self.prices:
                    if r[1] == 'pointlimit' and u.points >= r[2]:
                        mld += '|%s=ja' % r[0]
                        break
                mld += '}}\n'
            else:
                mld = ''
                for r in self.prices:
                    if r[1] == 'pointlimit' and u.points >= r[2]:
                        prizefound = True
                        if self.startweek == self.endweek:
                            mld += '{{UK deltaker|visuk=nei|år=%d|uke=%d|%s=ja}}\n' % (self.year, self.startweek, r[0])
                        else:
                            mld += '{{UK deltaker|visuk=nei|år=%d|uke=%d|ukefler=%d|%s=ja}}\n' % (self.year, self.startweek, self.endweek, r[0])
                        break

            now = datetime.now()
            yearweek = now.strftime('%Y-%W')
            mld += 'Husk at denne ukens konkurranse er [[Wikipedia:Ukens konkurranse/Ukens konkurranse %s|{{Ukens konkurranse liste|uke=%s}}]]. Lykke til! ' % (yearweek, yearweek)
            mld += 'Hilsen ' + ', '.join(['[[Bruker:%s|%s]]'%(s,s) for s in self.ledere]) + ' og ~~~~'

            if prizefound:
                page = self.sites['no'].pages['Brukerdiskusjon:' + u.name]
                log(' -> Leverer melding til %s' % page.name)
                page.save(text = mld, bot = False, section = 'new', summary = heading)

    def deliver_leader_notification(self, pagename):
        if self.startweek == self.endweek:
            heading = '== Ukens konkurranse uke %d == ' % self.startweek
        else:
            heading = '== Ukens konkurranse uke %d–%d == ' % (self.startweek, self.endweek)
        link = '//no.wikipedia.org/w/index.php?title=Bruker:UKBot/Premieutsendelse&action=edit&section=new&preload=Bruker:UKBot/Premieutsendelse/Preload&preloadtitle=send%20ut'
        for u in self.ledere:
            if self.startweek == self.endweek:
                mld = '{{UK arrangør|visuk=nei|år=%d|uke=%d|gul=ja}}\n' % (self.year, self.startweek)
            else:
                mld = '{{UK arrangør|visuk=nei|år=%d|uke=%d|ukefler=%d|gul=ja}}\n' % (self.year, self.startweek, self.endweek)
            mld += 'Du må nå sjekke resultatene. Hvis det er feilmeldinger nederst på [[%s|konkurransesiden]] må du sjekke om de relaterte bidragene har fått poengene de skal ha. Se også etter om det er kommentarer eller klager på diskusjonssiden. Hvis alt ser greit ut kan du trykke [%s her] (og lagre), så sender jeg ut rosetter ved første anledning. ' % (pagename, link)
            mld += 'Hilsen ~~~~'

            page = self.sites['no'].pages['Brukerdiskusjon:' + u]
            log(' -> Leverer arrangørmelding til %s' % page.name)
            page.save(text = mld, bot = False, section = 'new', summary = heading)
    
    def deliver_receipt_to_leaders(self):
        if self.startweek == self.endweek:
            heading = 'Ukens konkurranse uke %d' % self.startweek
        else:
            heading = 'Ukens konkurranse uke %d–%d' % (self.startweek, self.endweek)
        mld = '\n:Rosetter er nå [//no.wikipedia.org/w/index.php?title=Spesial%3ABidrag&contribs=user&target=UKBot&namespace=3 sendt ut]. ~~~~'
        for u in self.ledere:
            page = self.sites['no'].pages['Brukerdiskusjon:' + u]
            log(' -> Leverer kvittering til %s' % page.name)
            
            # Find section number
            txt = page.edit()
            sections = [s.strip() for s in re.findall('^[\s]*==([^=]+)==', txt, flags = re.M)]
            csection = sections.index(heading) + 1

            # Append text to section
            txt = page.edit(section = csection)
            page.save(appendtext = mld, bot = False, summary = heading)
    
    
    def delete_contribs_from_db(self):
        cur = self.sql.cursor()
        cur2 = self.sql.cursor()
        ts_start = self.start.astimezone(pytz.utc).strftime('%F %T')
        ts_end = self.end.astimezone(pytz.utc).strftime('%F %T')
        ndel = 0
        for row in cur.execute(u"SELECT site,revid,parentid FROM contribs WHERE timestamp >= ? AND timestamp <= ?", (ts_start, ts_end)):
            row2 = cur2.execute(u"DELETE FROM fulltexts WHERE site=? AND revid=?", [row[0],row[1]])
            ndel += row2.rowcount
            row2 = cur2.execute(u"DELETE FROM fulltexts WHERE site=? AND revid=?", [row[0],row[2]])
            ndel += row2.rowcount
        
        nremain = cur.execute('SELECT COUNT(*) FROM fulltexts').fetchone()[0]
        log('> Cleaned %d rows from fulltexts-table. %d rows remain' % (ndel, nremain))

        row = cur.execute(u"""DELETE FROM contribs WHERE timestamp >= ? AND timestamp <= ?""", (ts_start, ts_end))
        ndel = row.rowcount
        nremain = cur.execute('SELECT COUNT(*) FROM contribs').fetchone()[0]
        log('> Cleaned %d rows from contribs-table. %d rows remain' % (ndel, nremain))

        cur.close()
        cur2.close()
        self.sql.commit()

    def deliver_warnings(self):
        """
        Inform users about problems with their contribution(s)
        """
        cur = self.sql.cursor()
        for u in self.users:
            msgs = []
            if u.suspended_since != None:
                d = [self.name, u.name, 'suspension', '']
                if len( cur.execute(u'SELECT id FROM notifications WHERE contest=? AND user=? AND class=? AND args=?', d).fetchall() ) == 0:
                    msgs.append('Du er inntil videre suspendert fra konkurransen med virkning fra %s. Dette innebærer at dine bidrag gjort etter dette tidspunkt ikke teller i konkurransen, men alle bidrag blir registrert og skulle suspenderingen oppheves i løpet av konkurranseperioden vil også bidrag gjort i suspenderingsperioden telle med. Vi oppfordrer deg derfor til å arbeide med problemene som førte til suspenderingen slik at den kan oppheves.' % u.suspended_since.strftime('%e. %B %Y, %H:%M').decode('utf-8'))
                    cur.execute(u'INSERT INTO notifications (contest, user, class, args) VALUES (?,?,?,?)', d)
            for article_key, article in u.articles.iteritems():
                if article.disqualified:
                    d = [self.name, u.name, 'disqualified', article_key]
                    if len( cur.execute(u'SELECT id FROM notifications WHERE contest=? AND user=? AND class=? AND args=?', d).fetchall() ) == 0:
                        msgs.append('Bidragene dine til artikkelen [[%s|%s]] er diskvalifisert fra konkurransen. En diskvalifisering kan oppheves hvis du selv ordner opp i problemet som førte til diskvalifiseringen. Hvis andre brukere ordner opp i problemet er det ikke sikkert at den vil kunne oppheves.' % (':'+article_key, article.name))
                        cur.execute(u'INSERT INTO notifications (contest, user, class, args) VALUES (?,?,?,?)', d)
            
            if len(msgs) > 0:
                if self.startweek == self.endweek:
                    heading = '== Viktig informasjon angående Ukens konkurranse uke %d ==' % self.startweek
                else:
                    heading = '== Viktig informasjon angående Ukens konkurranse uke %d–%d ==' % (self.startweek, self.endweek)
                msg = 'Takk for innsatsen din i [[%(pagename)s|ukens konkurranses]] så langt. Det er dessverre registrert problemer med enkelte av dine bidrag som medfører at vi er nødt til å informere deg om følgende:\n' % { 'pagename': self.name }
                for m in msgs:
                    msg += '* %s\n' % m
                msg += 'Hvordan problemene kan løses kan diskuteres på konkurransens diskusjonsside. Du kan fjerne denne meldingen når du har lest den om du ønsker det. ~~~~'
                #print '------------------------------',u.name
                #print msg
                #print '------------------------------'

                page = self.sites['no'].pages['Brukerdiskusjon:' + u.name]
                log(' -> Leverer advarsel til %s' % page.name)
                page.save(text = msg, bot = False, section = 'new', summary = heading)
            self.sql.commit()


############################################################################################################################
# Main 
############################################################################################################################

if __name__ == '__main__':
    
    runstart = datetime.now()

    # "Hard" settings

    sites = {
        'no': Site('no.wikipedia.org'),
        'nn': Site('nn.wikipedia.org')
    }
    cpage = 'Bruker:UKBot/cat-ignore'
    
    # Login to increase api limit from 50 to 500 

    from wp_private import ukbotlogin
    for site in sites.itervalues():
        site.login(*ukbotlogin)
    del ukbotlogin
    
    # Read args

    parser = argparse.ArgumentParser( description = 'The UKBot' )
    parser.add_argument('--page', required=False, help='Name of the contest page to work with')
    parser.add_argument('--simulate', action='store_true', default=False, help='Do not write results to wiki')
    parser.add_argument('--output', nargs='?', default='', help='Write results to file')
    parser.add_argument('--log', nargs='?', default = '', help='Log file')
    parser.add_argument('--verbose', action='store_true', default=False, help='More verbose logging')
    parser.add_argument('--close', action='store_true', help='Close contest')
    args = parser.parse_args()

    if args.log != '':
        ukcommon.logfile = open(args.log, 'a')

    log('-----------------------------------------------------------------')
    log('UKBot starting at %s' % (runstart.strftime('%F %T')))

    sql = sqlite3.connect('uk.db')

    # Determine kpage

    if args.close:
        # Check if there are contests to be closed
        cur = sql.cursor()
        rows = cur.execute(u'SELECT name FROM contests WHERE ended=1 AND closed=0 LIMIT 1').fetchall()
        if len(rows) == 0:
            log(" -> Fant ingen konkurranser å avslutte!")
            sys.exit(0)
        cur.close()
        kpage = rows[0][0]
        log(" -> Contest %s is to be closed" % rows[0])
        lastrev = sites['no'].pages['Bruker:UKBot/Premieutsendelse'].revisions(prop='user|comment').next()
        closeuser = lastrev['user']
        revc = lastrev['comment']
        if revc != 'Nytt avsnitt: /* send ut */':
            log('>> Ikke klar til utsendelse')
            sys.exit(0)
    else:
        kpage = args.page

    # Is kpage redirect? Resolve

    log('@ kpage is %s' % kpage)
    pp = sites['no'].api('query', prop = 'pageprops', titles = kpage, redirects = '1')
    if 'redirects' in pp['query']:
        kpage = pp['query']['redirects'][0]['to']
        log('  -> Redirected to:  %s' % kpage)

    # Check that we're not given some very wrong page

    if not (re.match('^Wikipedia:Ukens konkurranse/Ukens konkurranse', kpage) or re.match('^Bruker:UKBot/Sandkasse', kpage)):
        raise StandardError('I refuse to work with that page!')

    # Initialize the contest

    try:
        uk = UK(sites['no'].pages[kpage], sites['no'].pages[cpage].edit(), sites, sql, verbose = args.verbose)
    except ParseError as e:
        err = "\n* '''%s'''" % e.msg
        page = sites['no'].pages[kpage]
        out = '\n{{Ukens konkurranse robotinfo | error | %s }}' % err
        page.save('dummy', summary = 'Resultatboten støtte på et problem', appendtext = out)
        raise
    
    if args.close and closeuser not in uk.ledere:
        log('!! Konkurransen ble forsøkt avsluttet av andre enn konkurranseleder')
        print '!! Konkurransen ble forsokt avsluttet av andre enn konkurranseleder'
        sys.exit(0)

    # Check if contest is to be ended
    
    log('@ Contest open from %s to %s' % (uk.start.strftime('%F %T'), uk.end.strftime('%F %T')))
    osl = pytz.timezone('Europe/Oslo')
    now = osl.localize(datetime.now())
    ending = False
    if args.close == False and now > uk.end:
        ending = True
        log("  -> Ending contest")
        cur = sql.cursor()
        if len(cur.execute(u'SELECT ended FROM contests WHERE name=? AND ended=1', [kpage] ).fetchall()) == 1:
            log("  -> Already ended. Abort")
            #print "Konkurransen kunne ikke avsluttes da den allerede er avsluttet"
            sys.exit(0)

        cur.close()

    # Loop over users

    narticles = 0
    nbytes = 0
    nwords = 0
    nnewpages = 0
    for u in uk.users:
        log("=== %s ===" % u.name)
        
        # First read contributions from db
        u.add_contribs_from_db(sql, uk.start, uk.end, sites)

        # Then fill in new contributions from wiki
        for site in sites.itervalues():
            u.add_contribs_from_wiki(site, uk.start, uk.end, fulltext = True)


        # And update db
        u.save_contribs_to_db(sql)

        try:

            # Filter out relevant articles
            u.filter(uk.filters)

            # And calculate points
            log(' -> Analyzing ', newline = False)
            u.analyze(uk.rules)
            log('OK')

            narticles += len(u.articles)
            nbytes += u.bytes
            nwords += u.words
            nnewpages += u.newpages

        except ParseError as e:
            err = "\n* '''%s'''" % e.msg
            page = sites['no'].pages[kpage]
            out = '\n{{Ukens konkurranse robotinfo | error | %s }}' % err
            if args.simulate:
                print out
            else:
                page.save('dummy', summary = 'Resultatboten støtte på et problem', appendtext = out)
            raise

    # Sort users by points

    uk.users.sort( key = lambda x: x.points, reverse = True )

    # Make outpage

    out = '== Resultater ==\n'
    out += '[[File:Nowp Ukens konkurranse %s.svg|thumb|400px|Resultater (oppdateres normalt hver natt i halv ett-tiden, viser kun de ti med høyest poengsum)]]\n' % uk.start.strftime('%Y-%W')
    

    sammen = '{{Ukens konkurranse status'
    
    ft = [type(f) for f in uk.filters]
    rt = [type(r) for r in uk.rules]

    if StubFilter in ft:
        sammen += '|avstubbet=%d' % narticles

    if ByteRule in rt or WordRule in rt:
        if nnewpages > 0:
            sammen += '|nye=%d' % nnewpages
        if nbytes >= 10000:
            sammen += '|kilobytes=%.f' % (nbytes/1000.)
        else:
            sammen += '|bytes=%d' % (nbytes)
        sammen += '|ord=%d' % (nwords)

    ts = [r for r in uk.rules if type(r) == RefRule]
    if len(ts) == 1:
        sammen += '|ref=%d' % (ts[0].totalsources)

    ts = [r for r in uk.rules if type(r) == TemplateRemovalRule]
    if len(ts) > 0:
        for i,r in enumerate(ts):
            sammen += '|mal%d=%s|mal%dn=%d' % (i+1, r.template, i+1, r.total)

    sammen += '}}'

    out += sammen + '\n'

    now = datetime.now()
    if ending:
        out += "''Konkurransen er nå avsluttet – takk til alle som deltok! Rosetter vil bli delt ut så snart konkurransearrangøren(e) har sjekket resultatene.''\n\n"
    elif args.close:
        out += "''Konkurransen er nå avsluttet – takk til alle som deltok!''\n\n"
    else:
        out += "''Sist oppdatert %s. Konkurransen er åpen fra %s til %s.''\n\n" % (now.strftime('%e. %B %Y, %H:%M').decode('utf-8'), uk.start.strftime('%e. %B %Y, %H:%M').decode('utf-8'), uk.end.strftime('%e. %B %Y, %H:%M').decode('utf-8'))

    for i,u in enumerate(uk.users):
        out += u.format_result( pos = i, closing = args.close, prices = uk.prices)


    article_errors = {}
    for u in uk.users:
        for article in u.articles.itervalues():
            k = article.site.key+':'+article.name
            if len(article.errors) > 0:
                article_errors[k] = article.errors
            for rev in article.revisions.itervalues():
                if len(rev.errors) > 0:
                    if k in article_errors:
                        article_errors[k].extend(rev.errors)
                    else:
                        article_errors[k] = rev.errors

    errors = []
    for art, err in article_errors.iteritems():
        if len(err) > 8:
            err = err[:8]
            err.append('(...)')
        errors.append('\n* Boten støtte på følgende problemer med artikkelen [[%s]]'%art + ''.join(['\n** %s' % e for e in err]))
    
    for site in uk.sites.itervalues():
        for error in site.errors:
            errors.append('\n* %s' % error)
    
    if len(errors) == 0:
        out += '{{Ukens konkurranse robotinfo | ok | %s }}' % now.strftime('%F %T')
    else:
        out += '{{Ukens konkurranse robotinfo | 1=note | 2=%s | 3=%s }}' % ( now.strftime('%F %T'), ''.join(errors) )
    
    out += '\n{{ukens konkurranse %s}}\n[[Kategori:Artikkelkonkurranser]]\n' % (uk.year)

    if not args.simulate:
        log(" -> Updating wiki, section = %d " % (uk.results_section))
        page = sites['no'].pages[kpage]
        if ending:
            page.save(out, summary = 'Oppdaterer med siste resultater og merker konkurransen som avsluttet', section = uk.results_section)
        elif args.close:
            page.save(out, summary = 'Kontrollerer og deler ut rosetter', section = uk.results_section)
        else:
            page.save(out, summary = 'Oppdaterer resultater', section = uk.results_section)

    if args.output != '':
        print "Writing output to file"
        f = codecs.open(args.output,'w','utf-8')
        f.write(out)
        f.close()

    if ending:
        log(" -> Ending contest")
        uk.deliver_leader_notification(kpage)

        page = sites['no'].pages['Bruker:UKBot/Premieutsendelse']
        page.save(text = 'venter', summary = 'Venter', bot = True)

        cur = sql.cursor()
        cur.execute(u'INSERT INTO contests (name, ended, closed) VALUES (?,1,0)', [kpage] )
        sql.commit()
        cur.close()
    
    if args.close:
        log(" -> Delivering prices")
        uk.deliver_prices()

        cur = sql.cursor()
        for u in uk.users:
            arg = [kpage, u.name, int(uk.startweek), u.points, int(u.bytes), int(u.newpages),'']
            if uk.startweek != uk.endweek:
                arg[-1] = int(uk.endweek)
            #print arg
            cur.execute(u"INSERT INTO users (contest, user, week, points, bytes, newpages, week2) VALUES (?,?,?,?,?,?,?)", arg )

        cur.execute(u'UPDATE contests SET closed=1 WHERE name=?', [kpage] )
        sql.commit()
        cur.close()

        page = sites['no'].pages['Bruker:UKBot/Premieutsendelse']
        page.save(text = 'sendt ut', summary = 'Sendt ut', bot = True)

        uk.deliver_receipt_to_leaders()


        log(" -> Cleaning DB")
        uk.delete_contribs_from_db()

    # Notify users about issues

    if not args.simulate:
        uk.deliver_warnings()

    # Update WP:UK

    if re.match('^Wikipedia:Ukens konkurranse/Ukens konkurranse', kpage) and not args.simulate and not args.close and not ending:
        page = sites['no'].pages['WP:UK']
        txt = '#OMDIRIGERING [[%s]]' % kpage
        if page.edit() != txt:
            page.save(txt, summary = 'Omdirigering til '+kpage)

    # Update Wikipedia:Portal/Oppslagstavle
    
    oppslagstavle = sites['no'].pages['Wikipedia:Portal/Oppslagstavle']
    txt = oppslagstavle.edit()

    dp = DanmicholoParser(txt)
    if len(dp.templates['la stå/uk']) != 1:
        raise StandardError(u'Feil: Fant %d la stå/uk-maler i Wikipedia:Portal/Oppslagstavle' % len(dp.templates['la stå/uk']))

    tpl = dp.templates['la stå/uk'][0]
    if int(tpl.parameters['uke']) != int(now.strftime('%W')):
        log('-> Oppdaterer Wikipedia:Portal/Oppslagstavle')
        tpl.parameters[1] = '{{subst:Ukens konkurranse liste|uke=%s}}' % now.strftime('%Y-%W')
        tpl.parameters['dato'] = now.strftime('%e. %h')
        tpl.parameters['år'] = now.strftime('%Y')
        tpl.parameters['uke'] = now.strftime('%W')
        txt2 = unicode(dp)
        oppslagstavle.save(txt2, summary = 'Oppdaterer ukens konkurranse')

    # Make a nice plot

    uk.plot()

    runend = datetime.now()
    runtime = (runend - runstart).total_seconds()
    log('UKBot finishing at %s. Runtime was %.f seconds.' % (runend.strftime('%F %T'), runtime))

