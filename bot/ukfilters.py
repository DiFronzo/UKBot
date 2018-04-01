# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import sys
import re
from copy import copy
from odict import odict
from ukcommon import t, _, InvalidContestPage
import logging
import time
import SPARQLWrapper

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class CategoryLoopError(Exception):
    """Raised when a category loop is found.

    Attributes:
        catpath -- category path followed while getting lost in the loop
    """
    def __init__(self, catpath):
        self.catpath = catpath
        self.msg = 'Entered a category loop'


class Filter(object):

    def __init__(self):
        pass

    def extend(self, ffilter):
        pass

#class StubFilter(Filter):
#    """ Filters articles that was stubs, but is no more """

#    def __init__(self):
#        Filter.__init__(self)

#    def is_stub(self, text):
#        """ Checks if a given text is a stub """

#        m = re.search(r'{{[^}]*(?:stubb|spire)[^}]*}}', text, re.IGNORECASE)
#        if m:
#            if self.verbose:
#                log(" >> %s " % m.group(0), newline = False)
#            return True
#        return False

    #def filter(self, articles):

    #    out = odict()
    #    for article_key, article in articles.items():

    #        firstrevid = article.revisions.firstkey()
    #        lastrevid = article.revisions.lastkey()

    #        firstrev = article.revisions[firstrevid]
    #        lastrev = article.revisions[lastrevid]

    #        try:

    #            # skip pages that are definitely not stubs to avoid timeconsuming parsing
    #            if article.new is False and article.redirect is False and len(firstrev.parenttext) < 20000:

    #                # Check if first revision is a stub
    #                if self.is_stub(firstrev.parenttext):

    #                    # Check if last revision is a stub
    #                    if not self.is_stub(lastrev.text):

    #                        out[article_key] = article

    #                    if self.verbose:
    #                        log('')

            #except DanmicholoParseError as e:
            #    log(" >> DanmicholoParser failed to parse " + article_key)
            #    parentid = firstrev.parentid
            #    args = { 'article': article_key, 'prevrev': firstrev.parentid, 'rev': lastrev.revid, 'error': e.msg }
            #    article.site().errors.append(_('Could not analyze the article %(article)s because one of the revisions %(prevrev)d or %(rev)d could not be parsed: %(error)s') % args)

    #    log("  [+] Applying stub filter: %d -> %d" % (len(articles), len(out)))

    #    return out


class TemplateFilter(Filter):
    """ Filters articles that had any of a given set of templates (or their aliases) at a point"""

    def __init__(self, templates, aliases=[]):
        Filter.__init__(self)
        templates.extend([a for a in aliases])
        self.templates = templates

    def extend(self, templatefilter):
        self.templates.extend(templatefilter.templates)

    def has_template(self, text):
        """ Checks if a given text contains the template"""

        tpls = [x.replace('*', '[^}]*?') for x in self.templates]
        m = re.search(r'{{(%s)[\s]*(\||}})' % '|'.join(tpls), text, flags=re.IGNORECASE)
        if m:
            return m.group(1)
        return None

    def filter(self, articles):

        out = odict()
        for article_key, article in articles.items():

            firstrevid = article.revisions.firstkey()
            firstrev = article.revisions[firstrevid]

            try:

                #if article.new == False and article.redirect == False:

                # Check if first revision is a stub
                t = self.has_template(firstrev.parenttext)
                if t:
                    logger.debug('Found template {{%s}} in [[%s]] @ %d',
                                 t, article_key, firstrevid)
                    out[article_key] = article

            except DanmicholoParseError as e:
                logger.warning(" >> DanmicholoParser failed to parse %s", article_key)
                parentid = firstrev.parentid
                args = {'article': article_key, 'prevrev': firstrev.parentid, 'rev': lastrev.revid, 'error': e.msg}
                article.site().errors.append(_('Could not analyze the article %(article)s because one of the revisions %(prevrev)d or %(rev)d could not be parsed: %(error)s') % args)

        logger.info(" - TemplateFilter: Articles reduced from %d to %d", len(articles), len(out))

        return out


class CatFilter(Filter):
    """ Filters articles that belong to a given overcategory """

    def __init__(self, sites, categories, maxdepth=5, ignore=[]):
        """
        Arguments:
            sites      : dict { 'no': <mwclient.client.Site>, ... }
            categories : list of Page objects
            maxdepth   : number of subcategory levels to traverse
            ignore     : list of categories to ignore
        """
        Filter.__init__(self)

        self.ignore = ignore
        self.sites = sites
        self.include = ['%s:%s' % (x.site.key, x.name) for x in categories]
        self.maxdepth = int(maxdepth)
        logger.debug("Initializing CatFilter: %s, maxdepth=%d",
                    " OR ".join(self.include), maxdepth)

    def extend(self, catfilter):
        self.include.extend(catfilter.include)

    def fetchcats(self, articles, debug=False):
        """ Fetches categories an overcategories for a set of articles """

        # Make a list of the categories of a given article, with one list for each level
        # > cats[article_key][level] = [cat1, cat2, ...]

        cats = {p: [[] for n in range(self.maxdepth + 1)] for p in articles}

        # Also, for each article, keep a list of category parents, so we can build
        # a path along the category tree from any matched category to the article
        # > parents[article_key][category] = parent_category
        #
        # Example:
        #                   /- cat 2
        #             /- cat1 -|
        # no:giraffe -|        \-
        #             \-
        #
        # parents['no:giraffe']['cat2'] = 'cat1'
        # parents['no:giraffe']['cat1'] = 'giraffe'
        #
        # We could also build full category trees for each article from the available
        # information, but they can grow quite big and slow to search

        parents = {p: {} for p in articles}

        #ctree = Tree()
        #for p in pages:
        #    ctree.add_child( name = p.encode('utf-8') )

        for site_key, site in self.sites.items():

            if 'bot' in site.rights:
                requestlimit = 500
                returnlimit = 5000
            else:
                requestlimit = 50
                returnlimit = 500

            # Titles of articles that belong to this site
            titles = [article.name for article in articles.values() if article.site().key == site_key]

            # logger.debug(' ['+site_key+':'+str(len(titles))+']')
            #.flush()
            if len(titles) > 0:

                for level in range(self.maxdepth + 1):

                    titles0 = copy(titles)
                    titles = []  # make a new list of titles to search
                    nc = 0
                    nnc = 0

                    for s0 in range(0, len(titles0), requestlimit):
                        logger.debug('[%d] > Getting %d to %d of %d', level, s0, s0+requestlimit, len(titles0))
                        ids = '|'.join(titles0[s0:s0+requestlimit])

                        cont = True
                        clcont = {'continue': ''}
                        while cont:
                            # print clcont
                            args = {'prop': 'categories', 'titles': ids, 'cllimit': returnlimit}
                            args.update(clcont)
                            q = site.api('query', **args)

                            if 'warnings' in q:
                                raise StandardError(q['warnings']['query']['*'])

                            for pageid, page in q['query']['pages'].items():
                                fulltitle = page['title']
                                shorttitle = fulltitle.split(':', 1)[-1]
                                article_key = site_key + ':' + fulltitle
                                if 'categories' in page:
                                    for cat in page['categories']:
                                        cat_title = cat['title']
                                        cat_short = cat_title.split(':', 1)[1]
                                        site_cat = site_key + ':' + cat_title
                                        follow = True
                                        for d in self.ignore:
                                            if re.search(d, cat_short):
                                                logger.debug(' - Ignore: "%s" matched "%s"', cat_title, d)
                                                follow = False
                                        if follow:
                                            nc += 1
                                            titles.append(cat_title)
                                            if level == 0:
                                                cats[article_key][level].append(site_cat)
                                                parents[article_key][site_cat] = article_key
                                                #print cat_short
                                                # use iter_search_nodes instead?
                                                #ctree.search_nodes( name = fulltitle.encode('utf-8') )[0].add_child( name = cat_short.encode('utf-8') )
                                            else:
                                                for article_key2, ccc in cats.items():
                                                    if article_key in ccc[level-1]:
                                                        ccc[level].append(site_cat)
                                                        parents[article_key2][site_cat] = article_key
                                                        # print '>',article_key2, ':', site_cat,' = ',article_key

                                                        #for node in ctree.search_nodes( name = shorttitle.encode('utf-8') ):
                                                        #    if not cat_short.encode('utf-8') in [i.name for i in node.get_children()]:
                                                        #        node.add_child(name = cat_short.encode('utf-8'))
                                        else:
                                            nnc += 1
                            if 'continue' in q:
                                clcont = q['continue']
                            else:
                                cont = False
                    titles = list(set(titles))  # to remove duplicates (not order preserving)
                    #if level == 0:
                    #    cattree = [p for p in titles]
                    logger.debug(' %d', len(titles))
                    #.stdout.flush()
                    #print "Found %d unique categories (%d total) at level %d (skipped %d categories)" % (len(titles), nc, level, nnc)
        
        return cats, parents

    def check_article_cats(self, article_cats):
        """ Checks if article_cats contains any of the cats given in self.include """
        # loop over levels
        for inc in self.include:
            for cats in article_cats:
                if inc in cats:
                    return inc
        return None

    def filter(self, articles, debug=False):

        cats, parents = self.fetchcats(articles, debug=debug)

        out = odict()

        # loop over articles
        for article_key, article_cats in cats.items():
            #if debug:
            #    print
            article = articles[article_key]
            lang = article_key.split(':')[0]
            if debug:
                logger.debug("CatFilter: %s", article.name)
                for l, ca in enumerate(article_cats):
                    logger.debug('CatFilter [%d] %s', l, ', '.join(ca))

            #print
            #print article_key
            #print article_cats
            #print
            catname = self.check_article_cats(article_cats)
            if catname:

                # Add category path to the article object, so we can check how the article matched
                article.cat_path = [catname]
                # print '[%s]' % (article_key)
                try:
                    i = 0
                    aname = article.site().key + ':' + article.name
                    while not catname == aname:
                        # print ' [%d] %s' % (i,catname)
                        if not parents[article_key][catname] == aname:
                            article.cat_path.append(parents[article_key][catname])
                        catname = parents[article_key][catname]
                        i += 1
                        if i > 50:
                            raise CategoryLoopError(article.cat_path)
                except CategoryLoopError as e:
                    article.errors.append(_('Encountered an infinite category loop: ')
                        + ' → '.join(['[[:%(catname)s|]]'
                        % {'catname': c} for c in e.catpath]))

                out[article_key] = article

        logger.info(" - CatFilter: Articles reduced from %d to %d", len(articles), len(out))
        return out


class ByteFilter(Filter):
    """Filters articles according to a byte treshold"""

    def __init__(self, bytelimit):
        Filter.__init__(self)
        self.bytelimit = int(bytelimit)

    def filter(self, articles):
        out = odict()
        for article_key, article in articles.items():
            if article.bytes >= self.bytelimit:
                out[article_key] = article
        logger.info(" - ByteFilter: Articles reduced from %d to %d",
                    len(articles), len(out))
        return out


class NewPageFilter(Filter):
    """Filters new articles"""

    def __init__(self, redirects=False):
        Filter.__init__(self)
        self.redirects = redirects

    def filter(self, articles):
        out = odict()
        for a, aa in articles.items():
            if not self.redirects and aa.new_non_redirect:
                out[a] = aa
            elif self.redirects and aa.new:
                out[a] = aa
        logger.info(" - NewPageFilter: Articles reduced from %d to %d", len(articles), len(out))
        return out


class ExistingPageFilter(Filter):
    """ Filters non-new articles """

    def __init__(self):
        Filter.__init__(self)

    def filter(self, articles):
        out = odict()
        for aname, article in articles.items():
            if not article.new:
                out[aname] = article
        logger.info(" - ExistingPageFilter: Articles reduced from %d -> %d", len(articles), len(out))
        return out


class BackLinkFilter(Filter):
    """Filters articles linked to from <self.links>"""

    def __init__(self, pages, site_from_prefix):
        """
        Arguments:
            pages  : list of Page objects
            site_from_prefix: fn
        """
        Filter.__init__(self)
        self.links = set()
        self.pages = pages
        self.site_from_prefix = site_from_prefix

        page_names = ['%s:%s' % (x.site.key, x.name) for x in pages]
        logger.info('Initializing BackLinkFilter: %s',
                    ','.join(page_names))

        for page in self.pages:
            for linked_page in page.links(namespace=0, redirects=True):
                link = '%s:%s' % (linked_page.site.key, linked_page.name.replace('_', ' '))
                logger.debug(' - Include: %s', link)
                self.links.add(link)

                # Include langlinks as well
                for langlink in linked_page.langlinks():
                    site = self.site_from_prefix(langlink[0])
                    if site is not None:
                        link = '%s:%s' % (site.host, langlink[1].replace('_', ' '))
                        logger.debug(' - Include: %s', link)
                        self.links.add(link)

        logger.info('BackLinkFilter ready with %d links (after having expanded langlinks)',
                    len(self.links))

    def extend(self, other_filter):
        self.pages.extend(other_filter.pages)
        for link in other_filter.links:
            self.links.add(link)

    def filter(self, articles):
        out = odict()
        for article_key, article in articles.items():
            if article_key in self.links:
                out[article_key] = article
        logger.info(' - BackLinkFilter: Articles reduced from %d to %d',
                    len(articles), len(out))
        return out


class ForwardLinkFilter(Filter):
    """Filters articles linking to <self.links>"""

    def __init__(self, pages, site_from_prefix):
        """
        Arguments:
            pages  : list of Page objects
            site_from_prefix: fn
        """
        Filter.__init__(self)
        self.links = set()
        self.pages = pages
        self.site_from_prefix = site_from_prefix

        for page in self.pages:
            for linked_page in page.backlinks(redirect=True):
                link = '%s:%s' % (linked_page.site.key, linked_page.name.replace('_', ' '))
                self.links.add(link)

        logger.info('ForwardLinkFilter ready with %d links', len(self.links))

    def extend(self, other_filter):
        self.pages.extend(other_filter.pages)
        for link in other_filter.links:
            self.links.add(link)

    def filter(self, articles):
        out = odict()
        for article_key, article in articles.items():
            if article_key in self.links:
                out[article_key] = article
        logger.info(" - ForwardLinkFilter: Articles reduced from %d to %d",
                    len(articles), len(out))
        return out


class PageFilter(Filter):
    """Filters articles with forwardlinks to <name>"""

    def __init__(self, pages):
        """
        Arguments:
            pages     : list of Page objects
        """
        Filter.__init__(self)
        self.pages = pages
        logger.info('PageFilter ready with %d links', len(self.pages))

    def extend(self, other_filter):
        self.pages.extend(other_filter.pages)

    def filter(self, articles):
        page_keys = ['%s:%s' % (page.site.key, page.name) for page in self.pages]
        out = odict()
        for article_key, article in articles.items():
            if article_key in page_keys:
                out[article_key] = article
        logger.info(' - PageFilter: Articles reduced from %d to %d',
                    len(articles), len(out))
        return out


class NamespaceFilter(Filter):
    """Filters articles with forwardlinks to <name>"""

    def __init__(self, namespaces, site=None):
        """
        Arguments:
            namespaces : list
        """
        Filter.__init__(self)
        self.namespaces = namespaces
        self.site = site

    def filter(self, articles):
        # Note: The .namespace property does not yet exist on the Article object!
        # out = odict()
        # for article_key, article in articles.items():
        #    if article.namespace == self.namespace:
        #        out[article_key] = article
        # log("  [+] Applying namespace filter (%s): %d -> %d" % (','.join(self.articles), len(articles), len(out)))
        # return articles
        return odict() # already filtered


class SparqlFilter(Filter):
    """Filters articles matching a SPARQL query"""

    def __init__(self, sites, query):
        """
        Arguments:
            pages     : list of Page objects
            sites     : list of site hostnames
        """
        Filter.__init__(self)
        self.sites = sites
        self.query = query
        self.fetch()

    def fetch(self):
        sparql = SPARQLWrapper.SPARQLWrapper2('http://query.wikidata.org/bigdata/namespace/wdq/sparql')
        logger.debug('SparqlFilter: %s', self.query)

        sparql.setQuery(self.query)
        t0 = time.time()
        try:
            result = sparql.query()
        except:
            raise InvalidContestPage(_('SPARQL query invalid or timed out'))
        dt = time.time() - t0

        if len(result.bindings) == 0:
            raise InvalidContestPage(_('SPARQL query returned zero results'))

        query_variable = result.variables[0]

        logger.info('SparqlFilter: Got %d results in %.1f secs', len(result.bindings), dt)

        articles = set()

        # Implementatio notes:
        # - When the contest includes multiple sites, we do one query per site. I tried using
        #   a single query with `VALUES ?site { %(sites)s }` instead, but the query time
        #   almost doubled for each additional site, making timeouts likely.
        # - I also tested doing two separate queries rather than one query with a subquery,
        #   but when the number of items became large it resulted in "request too large".
        for site in self.sites:
            article_variable = 'article19472065'  # "random string" to avoid matching anything in the subquery
            query = """
                SELECT ?%(article_variable)s
                WHERE {
                  { %(query)s }
                  ?%(article_variable)s schema:about ?%(query_variable)s .
                  ?%(article_variable)s schema:isPartOf <https://%(site)s/> .
                }
            """ % {
                'query_variable': query_variable,
                'article_variable': article_variable,
                'query': self.query,
                'site': site,
            }
            logger.debug('SparqlFilter: %s', query)
            sparql.setQuery(query)

            t0 = time.time()
            n = 0
            for res in sparql.query().bindings:
                n += 1
                article = '/'.join(res[article_variable].value.split('/')[4:])
                page_key = '%s:%s' % (site, article)
                articles.add(page_key)
                # logger.debug(page_key)

            dt = time.time() - t0
            logger.info('SparqlFilter: Got %d results for %s in %.1f secs', n, site, dt)
        self.articles = articles
        logger.info('SparqlFilter: Initialized with %d articles', len(self.articles))

    def filter(self, articles):
        out = odict()
        for article_key, article in articles.items():
            if article_key in self.articles:
                out[article_key] = article
        logger.info(' - SparqlFilter: Articles reduced from %d to %d',
                    len(articles), len(out))
        return out
