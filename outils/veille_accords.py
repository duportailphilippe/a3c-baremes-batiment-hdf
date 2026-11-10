#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Veille officielle DILA/KALI des petits déplacements Bâtiment HDF.

Ne modifie JAMAIS baremes.json. Sur une nouvelle publication potentiellement
pertinente, crée un ticket GitHub pour vérification juridique.

Ce programme lit l'index et les archives KALI diffusés par la DILA. Il ne
prétend pas garantir une recherche exhaustive des arrêtés d'extension au JORF.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ADRESSE_SOURCE = "https://echanges.dila.gouv.fr/OPENDATA/KALI/"
AGENT = "A3C-Veille-KALI/1.0 (utilisation professionnelle; contact par dépôt GitHub)"
DUREE_RESEAU = 40
TAILLE_MAX_ARCHIVE = 320_000_000
TAILLE_MAX_XML = 8_000_000
LIMITE_TICKETS = 12
# Le simulateur local possède déjà les barèmes connus jusqu'à l'accord du
# 10 juillet 2026. Les archives globales contiennent beaucoup de textes anciens.
DATE_DERNIER_ACCORD_CONNU = dt.date(2026, 7, 10)
MOIS = {
    'janvier': 1, 'fevrier': 2, 'mars': 3, 'avril': 4, 'mai': 5,
    'juin': 6, 'juillet': 7, 'aout': 8, 'septembre': 9,
    'octobre': 10, 'novembre': 11, 'decembre': 12,
}


def est_accord_deja_connu(titre: str) -> bool:
    """Écarte seulement les accords anciens explicitement datés dans leur titre."""
    t = normaliser(titre)
    m = re.search(r'accord du (1er|[0-3]?\d) (janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre) (20\d{2})', t)
    if not m:
        return False
    try:
        jour = 1 if m.group(1) == '1er' else int(m.group(1))
        date = dt.date(int(m.group(3)), MOIS[m.group(2)], jour)
    except (ValueError, KeyError):
        return False
    return date <= DATE_DERNIER_ACCORD_CONNU


def normaliser(texte: str) -> str:
    sans_accents = ''.join(c for c in unicodedata.normalize('NFKD', texte)
                           if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', sans_accents.lower()).strip()


class LecteurLiens(HTMLParser):
    def __init__(self):
        super().__init__()
        self.liens: list[str] = []

    def handle_starttag(self, balise, attributs):
        if balise == 'a':
            adresse = dict(attributs).get('href', '')
            if adresse:
                self.liens.append(adresse)


def repertoire_officiel(html_index: str) -> dict[str, str]:
    """Prend le dernier complément KALI et, si disponible, l'archive globale."""
    lecteur = LecteurLiens()
    lecteur.feed(html_index)
    complement = []
    globales = []
    for lien in lecteur.liens:
        nom = lien.rsplit('/', 1)[-1]
        if re.fullmatch(r'KALI_\d{8}-\d{6}\.tar\.gz', nom):
            complement.append(nom)
        elif re.fullmatch(r'Freemium_kali_global_\d{8}-\d{6}\.tar\.gz', nom, re.I):
            globales.append(nom)
    if not complement and not globales:
        raise ValueError('Aucune archive KALI reconnue dans le répertoire officiel DILA.')
    resultat = {}
    if complement:
        resultat['quotidien'] = urljoin(ADRESSE_SOURCE, max(complement))
    if globales:
        resultat['complet'] = urljoin(ADRESSE_SOURCE, max(globales))
    return resultat


def ouvrir(adresse: str):
    requete = Request(adresse, headers={'User-Agent': AGENT, 'Accept': '*/*'})
    return urlopen(requete, timeout=DUREE_RESEAU)


def lire_index() -> str:
    with ouvrir(ADRESSE_SOURCE) as reponse:
        contenu = reponse.read(200_000)
    return contenu.decode('latin-1', errors='replace')


def telecharger_archive(adresse: str) -> str:
    """Télécharge l'archive dans un fichier temporaire sans garder les 184 Mo en RAM."""
    temp = tempfile.NamedTemporaryFile(prefix='a3c_kali_', suffix='.tar.gz', delete=False)
    taille = 0
    try:
        with temp, ouvrir(adresse) as reponse:
            while True:
                morceau = reponse.read(1_048_576)
                if not morceau:
                    break
                taille += len(morceau)
                if taille > TAILLE_MAX_ARCHIVE:
                    raise ValueError('Archive officielle supérieure à la limite de 320 Mo.')
                temp.write(morceau)
        if taille < 100:
            raise ValueError('Archive officielle incomplète.')
        return temp.name
    except Exception:
        Path(temp.name).unlink(missing_ok=True)
        raise


def texte_xml(blob: bytes) -> tuple[str, str]:
    """Texte et titre d'un XML KALI ; extraction de secours sans exécuter de DTD."""
    titre = ''
    try:
        racine = ET.fromstring(blob)
        for element in racine.iter():
            nom = element.tag.rsplit('}', 1)[-1].upper() if isinstance(element.tag, str) else ''
            if nom in ('TITRE_TXT', 'TITRE', 'INTITULE') and (element.text or '').strip():
                if not titre:
                    titre = ' '.join(element.text.split())[:240]
        return ' '.join(racine.itertext()), titre
    except ET.ParseError:
        brut = blob.decode('utf-8', errors='replace')
        sans_balises = html.unescape(re.sub(r'<[^>]*>', ' ', brut))
        return sans_balises, ''


def est_pertinent(texte: str) -> bool:
    t = normaliser(texte)
    region = ('hauts-de-france' in t or 'hauts de france' in t)
    deplacement = ('deplacement' in t and 'indemnit' in t)
    petit = 'petits deplacements' in t or 'petit deplacement' in t
    code = bool(re.search(r'\b(?:idcc\s*[:n°-]?\s*)?(?:1596|1597)\b', t))
    batiment = 'batiment' in t and ('ouvrier' in t or 'entreprise' in t)
    # Les textes relatifs exclusivement aux travaux publics sont exclus.
    tp_seul = 'travaux publics' in t and not batiment and not code
    return region and deplacement and petit and (code or batiment) and not tp_seul


def pre_extraction(texte: str) -> dict:
    """Indices à valider, JAMAIS convertis directement en barème."""
    t = normaliser(texte)
    dates = sorted(set(re.findall(r'\b(?:0?[1-9]|[12]\d|3[01])[/.](?:0?[1-9]|1[0-2])[/.]20\d\d\b', t)))[:16]
    montants = sorted(set(re.findall(r'\b\d{1,3}(?:[.,]\d{1,2})\s*(?:€|euros?)(?![a-z0-9])', t)))[:30]
    zones = []
    for indice in range(1, 6):
        match = re.search(r'\bzone\s*(?:n[°o]\s*)?' + str(indice) + r'\b(.{0,150})', t)
        if match:
            portion = match.group(1).strip()
            valeurs = re.findall(r'\b\d{1,3}(?:[.,]\d{1,2})\s*(?:€|euros?)(?![a-z0-9])', portion)
            zones.append({'zone': indice, 'extrait': portion, 'montants_non_attribues': valeurs[:5]})
    return {'dates_reperees': dates, 'montants_reperes': montants, 'zones': zones}


def reference_legifrance(texte: str, chemin: str) -> str | None:
    source = texte + ' ' + chemin
    match = re.search(r'KALI(?:TEXT|ARTI)\d{10,}', source, re.I)
    if not match:
        return None
    identifiant = match.group(0).upper()
    nature = 'id' if identifiant.startswith('KALITEXT') else 'article'
    return f'https://www.legifrance.gouv.fr/conv_coll/{nature}/{identifiant}'


def analyser_archive(chemin: str, adresse_archive: str, limite_resultats=75) -> tuple[list[dict], int]:
    candidats: list[dict] = []
    vus: set[str] = set()
    compte = 0
    with tarfile.open(chemin, mode='r:gz') as archive:
        for membre in archive:
            if not membre.isfile() or not membre.name.lower().endswith('.xml'):
                continue
            compte += 1
            if membre.size > TAILLE_MAX_XML:
                continue
            fichier = archive.extractfile(membre)
            if fichier is None:
                continue
            contenu = fichier.read(TAILLE_MAX_XML + 1)
            texte, titre = texte_xml(contenu)
            if not est_pertinent(texte):
                continue
            if est_accord_deja_connu(titre):
                continue
            # Identifiant stable pour éviter les tickets répétés entre flux et archive complète.
            identifiant = re.search(r'KALI(?:TEXT|ARTI)\d{10,}', texte + ' ' + membre.name, re.I)
            signature = (identifiant.group(0).upper() if identifiant else membre.name)
            cle = hashlib.sha256((signature + '|' + normaliser(texte)).encode('utf-8')).hexdigest()[:16]
            if cle in vus:
                continue
            vus.add(cle)
            resume = ' '.join(texte.split())
            candidats.append({
                'cle': cle,
                'titre': titre or 'Publication KALI potentiellement relative aux indemnités de petits déplacements',
                'chemin_dans_archive': membre.name,
                'archive_officielle': adresse_archive,
                'texte_legifrance': reference_legifrance(texte, membre.name),
                'indices': pre_extraction(texte),
                'extrait': resume[:2500],
            })
            if len(candidats) >= limite_resultats:
                break
    return candidats, compte


def requete_github(adresse: str, jeton: str, methode='GET', donnees: dict | None = None):
    corps = json.dumps(donnees, ensure_ascii=False).encode('utf-8') if donnees is not None else None
    requete = Request(adresse, data=corps, method=methode,
                      headers={'Authorization': f'Bearer {jeton}',
                               'Accept': 'application/vnd.github+json',
                               'X-GitHub-Api-Version': '2022-11-28',
                               'User-Agent': AGENT,
                               'Content-Type': 'application/json'})
    with urlopen(requete, timeout=DUREE_RESEAU) as reponse:
        return json.load(reponse)


def tickets_existants(depot: str, jeton: str) -> set[str]:
    resultat = set()
    for page in range(1, 31):
        liste = requete_github(f'https://api.github.com/repos/{depot}/issues?state=all&per_page=100&page={page}',jeton)
        if not isinstance(liste, list):
            raise ValueError('Réponse inattendue de l’API GitHub (tickets).')
        for ticket in liste:
            if 'pull_request' not in ticket:
                cle = re.search(r'\[KALI:([0-9a-f]{16})\]', ticket.get('title', ''))
                if cle:
                    resultat.add(cle.group(1))
        if len(liste) < 100:
            break
    return resultat


def corps_ticket(candidat: dict) -> str:
    indices = candidat['indices']
    morceaux = [
        '## Publication officielle détectée — contrôle juridique requis',
        '',
        '**Aucune mise à jour du barème public n’a été effectuée.**',
        '',
        f"- Titre extrait : {candidat['titre']}",
        f"- Archive DILA : {candidat['archive_officielle']}",
        f"- Fichier dans l’archive : `{candidat['chemin_dans_archive']}`",
        f"- Légifrance (si identifiable) : {candidat['texte_legifrance'] or 'Identifiant non extrait'}",
        f"- Dates repérées : {', '.join(indices['dates_reperees']) or 'Aucune'}",
        f"- Montants repérés (non attribués) : {', '.join(indices['montants_reperes']) or 'Aucun'}",
        '', '### Repérage brut par zone (ordre et nature des montants à vérifier)',
        '',
        '| Zone | Montants présents à proximité (non attribués) |',
        '|---|---|',
        *[f"| {z['zone']} | {', '.join(z['montants_non_attribues']) or 'Non extrait'} |" for z in indices['zones']],
        '', '### Extrait automatiquement préparé (non certifié)',
        '', '> ' + candidat['extrait'].replace('\n', ' ').replace('>', '›'),
        '', '### Contrôles obligatoires avant publication',
        '- [ ] Texte effectivement relatif aux ouvriers du bâtiment (IDCC 1596 et/ou 1597) en Hauts-de-France.',
        '- [ ] Accord nouveau ou rectificatif, et articulation avec les accords précédents.',
        '- [ ] Organisation(s) patronale(s) signataire(s) et date d’effet.',
        '- [ ] Arrêté d’extension, publication au Journal officiel et date d’opposabilité.',
        '- [ ] Montants repas, transport et trajet des cinq zones, dans le bon ordre.',
        '- [ ] Validation par un professionnel avant ajout manuel à `baremes.json`.',
        '', 'Ce ticket est un **signal de veille**, non un accord validé ni un droit individuel à indemnisation.',
    ]
    return '\n'.join(morceaux)[:60000]


def publier_tickets(candidats: list[dict], depot: str, jeton: str) -> tuple[int, int]:
    existants = tickets_existants(depot, jeton)
    nouveaux = 0
    ignores = 0
    for candidat in candidats:
        cle = candidat['cle']
        if cle in existants:
            ignores += 1
            continue
        if nouveaux >= LIMITE_TICKETS:
            print(f'Attention : limite de {LIMITE_TICKETS} tickets atteinte, consulter le rapport intégral.')
            break
        titre = f"[KALI:{cle}] À vérifier — {candidat['titre'][:110]}"
        requete_github(f'https://api.github.com/repos/{depot}/issues',jeton,'POST',
                       {'title': titre, 'body': corps_ticket(candidat)})
        print('Ticket créé :', titre)
        nouveaux += 1
    return nouveaux, ignores


def execution(argv=None) -> int:
    parseur = argparse.ArgumentParser(description='Veille DILA/KALI du bâtiment HDF, sans modification des barèmes publiés.')
    parseur.add_argument('--mode', choices=['quotidien', 'complet'], default='quotidien')
    parseur.add_argument('--archive-locale', help='Archive tar.gz de test : évite tout téléchargement')
    parseur.add_argument('--rapport', default='rapport_veille.json')
    parseur.add_argument('--creer-tickets', action='store_true')
    arguments = parseur.parse_args(argv)
    try:
        if arguments.archive_locale:
            adresse = 'fichier local de test'
            chemin = arguments.archive_locale
        else:
            liens = repertoire_officiel(lire_index())
            if arguments.mode not in liens:
                raise ValueError(f"Archive « {arguments.mode} » absente du répertoire DILA.")
            adresse = liens[arguments.mode]
            chemin = telecharger_archive(adresse)
        try:
            candidats, compte = analyser_archive(chemin, adresse)
        finally:
            if not arguments.archive_locale:
                Path(chemin).unlink(missing_ok=True)
        rapport = {
            'source': ADRESSE_SOURCE,
            'archive_controlee': adresse,
            'mode': arguments.mode,
            'controle_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'fichiers_xml_examines': compte,
            'nombre_documents_a_verifier': len(candidats),
            'documents': candidats,
            'avertissement': 'Détection documentaire partielle : absence de résultat ≠ absence de nouvel accord ou arrêté d’extension.',
            'baremes_modifies': False,
        }
        Path(arguments.rapport).write_text(json.dumps(rapport, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Archive contrôlée : {adresse}")
        print(f"XML examinés : {compte}. Publications à examiner : {len(candidats)}.")
        if arguments.creer_tickets:
            depot = os.getenv('GITHUB_REPOSITORY', '')
            jeton = os.getenv('GITHUB_TOKEN', '')
            if not depot or not jeton:
                raise ValueError('Jeton GitHub ou nom de dépôt manquant : aucun ticket créé.')
            nouveaux, ignores = publier_tickets(candidats, depot, jeton)
            print(f'Tickets créés : {nouveaux}. Déjà signalés : {ignores}.')
        print('Le fichier baremes.json reste inchangé.')
        return 0
    except (OSError, ValueError, HTTPError, URLError, tarfile.TarError) as erreur:
        print('ÉCHEC DE LA VEILLE :', erreur, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(execution())
